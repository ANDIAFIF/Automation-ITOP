"""
sync.py — iTop → SOC Activity Dashboard one-way sync agent.

Modes:
    --seed              Set HWM = sekarang tanpa push (abaikan history iTop).
                        WAJIB dijalankan sekali saat first deploy.
    --backfill-days N   Set HWM = sekarang - N hari, lalu langsung run 1 cycle.
    --once              Run 1x poll cycle lalu exit (cocok untuk cron / debug).
    --loop              Loop terus tiap POLL_INTERVAL_SECONDS detik.
                        Tiap cycle juga memproses antrian sync periode dari
                        dashboard (GET /api/integrations/itop/sync-requests).
    --period S E        Sync SEMUA tiket periode start_date [S, E] ke cache
                        dashboard — independen dari HWM (HWM tidak disentuh).
                        Batas boleh tanggal saja ('2026-07-20') atau tanggal+jam
                        ('2026-07-20 08:00', pakai tanda kutip). Untuk
                        pencocokan laporan BiWeekly.
    --client CODE       (dengan --period) hanya push klien dashboard tsb.
    --dry-run           Fetch + mapping saja, jangan POST ke dashboard,
                        HWM tidak maju.
    --class <name>      Restrict ke 1 class iTop (default: UserRequest + Incident).

Behavior:
- Delta polling per class via sync_state.last_update_hwm (inklusif >=,
  dedup re-fetch via payload hash di synced_tickets).
- HWM hanya maju setelah dashboard ACK batch tanpa error.
- Org iTop yang tidak ada di clients_map.yaml → skip + WARN, HWM tetap maju.
- Gagal di satu class tidak memblok class lain.

Catatan: --seed/--backfill pakai jam lokal PC — pastikan clock PC dan server
iTop sama-sama WIB (atau selisihnya kecil).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import state
from config import get_org_mappings, settings
from dashboard_client import DashboardClient, DashboardError
from itop_client import ItopClient, ItopError
from mapping import normalize_ticket, payload_hash

log = logging.getLogger("itop_sync")

TICKET_CLASSES = ["UserRequest", "Incident"]


def _now_itop() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------------------------------------------------------
# Core: 1 cycle untuk 1 class
# -----------------------------------------------------------------------------
def poll_class(
    itop_class: str,
    itop: ItopClient,
    dashboard: DashboardClient,
    dry_run: bool,
) -> dict[str, int]:
    stats = {"fetched": 0, "pushed": 0, "unchanged": 0, "unmapped": 0, "errors": 0}
    mappings = get_org_mappings()

    hwm = state.get_hwm(itop_class)
    if hwm is None:
        log.error(
            "[%s] Belum ada HWM — jalankan dulu `python sync.py --seed` "
            "atau `--backfill-days N`",
            itop_class,
        )
        stats["errors"] += 1
        return stats

    # Re-poll langsung kalau hasil == limit (masih ada sisa di belakang)
    while True:
        try:
            tickets = itop.get_tickets(itop_class, since=hwm, limit=settings.batch_limit)
        except ItopError as e:
            log.error("[%s] iTop error: %s", itop_class, e)
            stats["errors"] += 1
            return stats

        stats["fetched"] += len(tickets)
        if not tickets:
            state.touch_poll(itop_class)
            log.info("[%s] Tidak ada update (hwm=%s)", itop_class, hwm)
            return stats

        batch: list[dict] = []
        batch_meta: list[tuple[int, str | None, str | None, str]] = []
        max_last_update = hwm

        for t in tickets:
            last_update = t["fields"].get("last_update") or ""
            if last_update > max_last_update:
                max_last_update = last_update

            payload = normalize_ticket(t, mappings)
            if payload is None:
                log.warning(
                    "[%s] #%s org tidak ter-mapping (org_id=%s org_name=%r) — skip",
                    itop_class, t["id"],
                    t["fields"].get("org_id"), t["fields"].get("org_name"),
                )
                stats["unmapped"] += 1
                continue

            phash = payload_hash(payload)
            if state.get_payload_hash(itop_class, t["id"]) == phash:
                stats["unchanged"] += 1
                continue

            batch.append(payload)
            batch_meta.append((t["id"], payload["ticket_itop"], last_update or None, phash))

        if dry_run:
            for p in batch:
                log.info("[%s] [DRY-RUN] would push %s status=%s -> %s",
                         itop_class, p["ticket_itop"], p["itop_status"], p["event_status"])
            log.info("[%s] [DRY-RUN] %d ticket akan di-push, HWM tidak diubah", itop_class, len(batch))
            return stats

        if batch:
            try:
                results = dashboard.push(batch, overwrite_mode=settings.overwrite_mode)
            except DashboardError as e:
                # Jangan advance HWM — cycle berikutnya retry dari titik yang sama
                log.error("[%s] Push ke dashboard gagal: %s (HWM tetap %s)", itop_class, e, hwm)
                stats["errors"] += 1
                return stats

            for (tid, ref, last_update, phash), res in zip(batch_meta, results):
                state.record_synced(itop_class, tid, ref, last_update, phash)
                log.info("[%s] %s -> %s", itop_class, ref, res.get("action"))
            stats["pushed"] += len(batch)

        state.set_hwm(itop_class, max_last_update)
        log.info(
            "[%s] Done: fetched=%d pushed=%d unchanged=%d unmapped=%d (hwm=%s)",
            itop_class, stats["fetched"], stats["pushed"],
            stats["unchanged"], stats["unmapped"], max_last_update,
        )

        if len(tickets) >= settings.batch_limit:
            log.warning(
                "[%s] Hasil == limit (%d) — kemungkinan masih ada sisa, re-poll langsung",
                itop_class, settings.batch_limit,
            )
            hwm = max_last_update
            continue
        return stats


# -----------------------------------------------------------------------------
# Sync periode (laporan BiWeekly) — SEMUA tiket dalam rentang start_date,
# independen dari HWM. Dipanggil dari CLI --period atau antrian dashboard.
# -----------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _normalize_time(raw: str | None, default: str) -> str:
    """'HH:MM' / 'HH:MM:SS' (postgres time) / None → 'HH:MM'."""
    t = (raw or "").strip()[:5]
    return t if t else default


def run_period_sync(
    itop: ItopClient,
    dashboard: DashboardClient,
    period_start: str,
    period_end: str,
    client_code: str | None = None,
    classes: list[str] | None = None,
    dry_run: bool = False,
    start_time: str = "00:00",
    end_time: str = "23:59",
) -> dict[str, int]:
    """
    Fetch semua tiket periode (tanggal + jam) dari iTop, normalize, push ke
    cache dashboard. HWM TIDAK disentuh. Return stats {fetched, pushed,
    unmapped, skipped_client}.
    Raise ItopError/DashboardError/ValueError kalau gagal — caller yang lapor.
    """
    # tanggal & jam dipakai literal di OQL — tolak format aneh (termasuk dari antrian)
    for label, val in (("period_start", period_start), ("period_end", period_end)):
        if not _DATE_RE.match(val or ""):
            raise ValueError(f"{label} harus YYYY-MM-DD, dapat: {val!r}")
    for label, val in (("start_time", start_time), ("end_time", end_time)):
        if not _TIME_RE.match(val or ""):
            raise ValueError(f"{label} harus HH:MM, dapat: {val!r}")

    mappings = get_org_mappings()
    stats = {"fetched": 0, "pushed": 0, "unmapped": 0, "skipped_client": 0}

    for cls in classes or TICKET_CLASSES:
        tickets = itop.get_tickets_by_period(
            cls, period_start, period_end, limit=settings.batch_limit,
            start_time=start_time, end_time=end_time,
        )
        stats["fetched"] += len(tickets)

        batch: list[dict] = []
        batch_meta: list[tuple[int, str | None, str | None, str]] = []
        for t in tickets:
            payload = normalize_ticket(t, mappings)
            if payload is None:
                stats["unmapped"] += 1
                continue
            if client_code and payload["client_code"] != client_code:
                stats["skipped_client"] += 1
                continue
            batch.append(payload)
            batch_meta.append(
                (t["id"], payload["ticket_itop"],
                 t["fields"].get("last_update") or None, payload_hash(payload))
            )

        if dry_run:
            for p in batch:
                log.info("[%s] [DRY-RUN] period sync would push %s (%s)",
                         cls, p["ticket_itop"], p["event_status"])
            continue

        # push per batch_limit — cache upsert idempotent, aman di-push ulang
        for i in range(0, len(batch), settings.batch_limit):
            chunk = batch[i : i + settings.batch_limit]
            dashboard.push(chunk, overwrite_mode=settings.overwrite_mode)
            for tid, ref, last_update, phash in batch_meta[i : i + settings.batch_limit]:
                state.record_synced(cls, tid, ref, last_update, phash)
            stats["pushed"] += len(chunk)
        log.info("[%s] Period sync %s %s..%s %s: fetched=%d pushed(kumulatif)=%d",
                 cls, period_start, start_time, period_end, end_time,
                 len(tickets), stats["pushed"])

    return stats


def process_sync_requests(
    itop: ItopClient, dashboard: DashboardClient, dry_run: bool
) -> None:
    """
    Ambil antrian sync periode yang dibuat analis di dashboard (tombol
    "Sync iTop" halaman Generate Laporan), eksekusi, lalu lapor statusnya.
    Gagal di satu request tidak memblok request lain.
    """
    try:
        requests_ = dashboard.fetch_sync_requests()
    except DashboardError as e:
        log.warning("Gagal ambil antrian sync: %s", e)
        return
    if not requests_:
        return

    for req in requests_:
        rid = req.get("id")
        start = req.get("period_start") or ""
        end = req.get("period_end") or ""
        start_time = _normalize_time(req.get("period_start_time"), "00:00")
        end_time = _normalize_time(req.get("period_end_time"), "23:59")
        code = req.get("client_code") or None
        log.info("Sync request %s: klien=%s periode=%s %s..%s %s",
                 rid, code or "SEMUA", start, start_time, end, end_time)

        if dry_run:
            log.info("[DRY-RUN] sync request %s tidak dieksekusi", rid)
            continue

        try:
            dashboard.update_sync_request(rid, "running")
            stats = run_period_sync(
                itop, dashboard, start, end, client_code=code,
                start_time=start_time, end_time=end_time,
            )
            dashboard.update_sync_request(rid, "done", stats=stats)
            log.info("Sync request %s selesai: %s", rid, stats)
        except Exception as e:  # lapor error ke dashboard, jangan matikan loop
            log.exception("Sync request %s gagal: %s", rid, e)
            try:
                dashboard.update_sync_request(rid, "error", error=str(e)[:500])
            except DashboardError as e2:
                log.error("Gagal lapor error sync request %s: %s", rid, e2)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def run(
    mode: str,
    classes: list[str],
    dry_run: bool,
    backfill_days: int | None,
    period: tuple[str, str] | None = None,
    client_code: str | None = None,
) -> int:
    state.init_db()
    get_org_mappings()  # fail-fast kalau clients_map.yaml rusak

    if mode == "seed":
        now = _now_itop()
        for cls in classes:
            state.set_hwm(cls, now)
            log.info("[%s] SEED: HWM di-set ke %s", cls, now)
        return 0

    if backfill_days is not None:
        start = (datetime.now() - timedelta(days=backfill_days)).strftime("%Y-%m-%d %H:%M:%S")
        for cls in classes:
            state.set_hwm(cls, start)
            log.info("[%s] BACKFILL: HWM di-set ke %s (%d hari)", cls, start, backfill_days)

    itop = ItopClient.from_settings()
    dashboard = DashboardClient.from_settings()

    if mode == "period":
        assert period is not None
        # tiap batas boleh 'YYYY-MM-DD' atau 'YYYY-MM-DD HH:MM'
        start_date, _, start_time = period[0].partition(" ")
        end_date, _, end_time = period[1].partition(" ")
        try:
            stats = run_period_sync(
                itop, dashboard, start_date, end_date,
                client_code=client_code, classes=classes, dry_run=dry_run,
                start_time=start_time or "00:00", end_time=end_time or "23:59",
            )
        except (ItopError, DashboardError, ValueError) as e:
            log.error("Period sync gagal: %s", e)
            return 1
        log.info("Period sync %s..%s selesai: %s", period[0], period[1], stats)
        return 0

    def _one_cycle() -> None:
        for cls in classes:
            try:
                poll_class(cls, itop, dashboard, dry_run)
            except Exception as e:
                log.exception("[%s] Unhandled error di poll cycle: %s", cls, e)
        # antrian sync periode dari dashboard (tombol "Sync iTop" di UI laporan)
        process_sync_requests(itop, dashboard, dry_run)

    if mode == "once":
        _one_cycle()
        return 0

    # Loop mode
    interval = settings.poll_interval_seconds
    log.info("Loop mode: interval=%ds classes=%s overwrite=%s",
             interval, classes, settings.overwrite_mode)
    while True:
        try:
            _one_cycle()
        except KeyboardInterrupt:
            log.info("Interrupted, exiting")
            return 0
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="iTop -> Activity Dashboard sync agent")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed", action="store_true", help="Set HWM = sekarang tanpa push")
    g.add_argument("--once", action="store_true", help="Run 1x cycle lalu exit")
    g.add_argument("--loop", action="store_true", help="Loop terus tiap POLL_INTERVAL_SECONDS")
    g.add_argument("--period", nargs=2, metavar=("START", "END"), default=None,
                   help="Sync semua tiket periode start_date [START, END] — "
                        "'YYYY-MM-DD' atau 'YYYY-MM-DD HH:MM' (pakai kutip); HWM tidak disentuh")

    parser.add_argument("--backfill-days", type=int, default=None,
                        help="Reset HWM ke N hari lalu sebelum run")
    parser.add_argument("--client", default=None,
                        help="(dengan --period) hanya push klien dashboard code tsb, mis. AF")
    parser.add_argument("--dry-run", action="store_true",
                        help="Jangan POST ke dashboard, HWM tidak maju")
    parser.add_argument("--class", dest="itop_class", default=None,
                        choices=TICKET_CLASSES, help="Restrict ke 1 class iTop")
    parser.add_argument("--log-level", default=settings.log_level)
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        log_path = Path(settings.log_file)
        if not log_path.is_absolute():
            log_path = Path(__file__).resolve().parent / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    if args.seed:
        mode = "seed"
    elif args.once:
        mode = "once"
    elif args.period:
        mode = "period"
    else:
        mode = "loop"
    classes = [args.itop_class] if args.itop_class else TICKET_CLASSES
    period = (args.period[0], args.period[1]) if args.period else None
    return run(mode, classes, args.dry_run, args.backfill_days,
               period=period, client_code=args.client)


if __name__ == "__main__":
    sys.exit(main())
