"""
sync.py — iTop → SOC Activity Dashboard one-way sync agent.

Modes:
    --seed              Set HWM = sekarang tanpa push (abaikan history iTop).
                        WAJIB dijalankan sekali saat first deploy.
    --backfill-days N   Set HWM = sekarang - N hari, lalu langsung run 1 cycle.
    --once              Run 1x poll cycle lalu exit (cocok untuk cron / debug).
    --loop              Loop terus tiap POLL_INTERVAL_SECONDS detik.
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
# Main
# -----------------------------------------------------------------------------
def run(mode: str, classes: list[str], dry_run: bool, backfill_days: int | None) -> int:
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

    def _one_cycle() -> None:
        for cls in classes:
            try:
                poll_class(cls, itop, dashboard, dry_run)
            except Exception as e:
                log.exception("[%s] Unhandled error di poll cycle: %s", cls, e)

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

    parser.add_argument("--backfill-days", type=int, default=None,
                        help="Reset HWM ke N hari lalu sebelum run")
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

    mode = "seed" if args.seed else ("once" if args.once else "loop")
    classes = [args.itop_class] if args.itop_class else TICKET_CLASSES
    return run(mode, classes, args.dry_run, args.backfill_days)


if __name__ == "__main__":
    sys.exit(main())
