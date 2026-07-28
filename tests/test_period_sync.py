"""Unit tests fitur sync periode (laporan BiWeekly) — pytest tests/ -v"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync
from config import OrgMapping
from mapping import normalize_ticket

MAPPINGS = [
    OrgMapping(itop_org_id=27, itop_org_name="PT Bio Farma (Persero)", dashboard_code="BIOFARMA"),
    OrgMapping(itop_org_id=None, itop_org_name="Harita Kencana", dashboard_code="AF"),
]


def _ticket(tid: int, org: str, ref: str, desc: str | None = "<p>IP: 1.2.3.4</p>") -> dict:
    return {
        "id": tid,
        "class": "UserRequest",
        "fields": {
            "ref": ref,
            "title": f"Alert {ref}",
            "description": desc,
            "status": "resolved",
            "org_id": None,
            "org_name": org,
            "start_date": "2026-07-21 10:00:00",
            "last_update": "2026-07-22 09:00:00",
            "resolution_date": "2026-07-22 09:00:00",
            "priority": "3",
        },
    }


# -----------------------------------------------------------------------------
# normalize_ticket: description mentah ikut ke payload (kolom cache dashboard)
# -----------------------------------------------------------------------------
def test_normalize_includes_raw_description():
    payload = normalize_ticket(_ticket(1, "Harita Kencana", "R-000001"), MAPPINGS)
    assert payload is not None
    assert payload["description"] == "<p>IP: 1.2.3.4</p>"


def test_normalize_description_none_and_cap():
    p_none = normalize_ticket(_ticket(1, "Harita Kencana", "R-000001", desc=None), MAPPINGS)
    assert p_none is not None and p_none["description"] is None
    p_long = normalize_ticket(
        _ticket(2, "Harita Kencana", "R-000002", desc="x" * 50_000), MAPPINGS
    )
    assert p_long is not None and len(p_long["description"]) == 20_000


# -----------------------------------------------------------------------------
# run_period_sync
# -----------------------------------------------------------------------------
class FakeItop:
    def __init__(self, tickets: list[dict]):
        self.tickets = tickets
        self.calls: list[tuple] = []

    def get_tickets_by_period(self, cls, start, end, limit=100,
                              start_time="00:00", end_time="23:59"):
        self.calls.append((cls, start, end, start_time, end_time))
        return [t for t in self.tickets if t["class"] == cls]


class FakeDashboard:
    def __init__(self):
        self.pushed: list[dict] = []

    def push(self, batch, overwrite_mode="full"):
        self.pushed.extend(batch)
        return [{"ticket_itop": p["ticket_itop"], "action": "ok", "error": None} for p in batch]


@pytest.fixture(autouse=True)
def _no_sqlite(monkeypatch):
    monkeypatch.setattr(sync.state, "record_synced", lambda *a, **k: None)
    monkeypatch.setattr(sync, "get_org_mappings", lambda: MAPPINGS)


def test_period_sync_pushes_all_mapped(monkeypatch):
    itop = FakeItop([
        _ticket(1, "Harita Kencana", "R-000001"),
        _ticket(2, "PT Bio Farma (Persero)", "R-000002"),
        _ticket(3, "Org Tak Dikenal", "R-000003"),
    ])
    dash = FakeDashboard()
    stats = sync.run_period_sync(itop, dash, "2026-07-20", "2026-07-27")
    assert stats["fetched"] == 3
    assert stats["pushed"] == 2
    assert stats["unmapped"] == 1
    # kedua class di-query
    assert {c[0] for c in itop.calls} == {"UserRequest", "Incident"}


def test_period_sync_filter_client():
    itop = FakeItop([
        _ticket(1, "Harita Kencana", "R-000001"),
        _ticket(2, "PT Bio Farma (Persero)", "R-000002"),
    ])
    dash = FakeDashboard()
    stats = sync.run_period_sync(itop, dash, "2026-07-20", "2026-07-27", client_code="AF")
    assert stats["pushed"] == 1
    assert stats["skipped_client"] == 1
    assert dash.pushed[0]["client_code"] == "AF"


def test_period_sync_rejects_bad_dates():
    with pytest.raises(ValueError):
        sync.run_period_sync(FakeItop([]), FakeDashboard(), "2026-07-20", "27-07-2026")
    with pytest.raises(ValueError):
        sync.run_period_sync(FakeItop([]), FakeDashboard(), "2026-07-20' OR 1=1 --", "2026-07-27")


def test_period_sync_jam_diteruskan_dan_divalidasi():
    itop = FakeItop([_ticket(1, "Harita Kencana", "R-000001")])
    sync.run_period_sync(itop, FakeDashboard(), "2026-07-20", "2026-07-27",
                         start_time="08:00", end_time="17:30")
    assert itop.calls[0][3:] == ("08:00", "17:30")
    # jam rusak / injection ditolak
    with pytest.raises(ValueError):
        sync.run_period_sync(FakeItop([]), FakeDashboard(), "2026-07-20", "2026-07-27",
                             start_time="25:00")
    with pytest.raises(ValueError):
        sync.run_period_sync(FakeItop([]), FakeDashboard(), "2026-07-20", "2026-07-27",
                             end_time="17:30' OR 1=1")


def test_normalize_time():
    assert sync._normalize_time("08:00:00", "00:00") == "08:00"  # postgres time
    assert sync._normalize_time("17:30", "00:00") == "17:30"
    assert sync._normalize_time(None, "23:59") == "23:59"
    assert sync._normalize_time("", "00:00") == "00:00"


def test_period_sync_dry_run_pushes_nothing():
    itop = FakeItop([_ticket(1, "Harita Kencana", "R-000001")])
    dash = FakeDashboard()
    stats = sync.run_period_sync(itop, dash, "2026-07-20", "2026-07-27", dry_run=True)
    assert dash.pushed == []
    assert stats["pushed"] == 0
    assert stats["fetched"] == 1  # fake hanya mengembalikan tiket sesuai class


# -----------------------------------------------------------------------------
# process_sync_requests — status dilaporkan balik, gagal 1 tidak memblok
# -----------------------------------------------------------------------------
class FakeDashboardQueue(FakeDashboard):
    def __init__(self, requests_):
        super().__init__()
        self.requests_ = requests_
        self.updates: list[tuple] = []

    def fetch_sync_requests(self):
        return self.requests_

    def update_sync_request(self, rid, status, stats=None, error=None):
        self.updates.append((rid, status, stats, error))


def test_process_sync_requests_done_and_error():
    itop = FakeItop([_ticket(1, "Harita Kencana", "R-000001")])
    dash = FakeDashboardQueue([
        {"id": "req-1", "period_start": "2026-07-20", "period_end": "2026-07-27",
         "client_code": "AF", "period_start_time": "08:00:00", "period_end_time": "17:30:00"},
        {"id": "req-2", "period_start": "TANGGAL-RUSAK", "period_end": "2026-07-27", "client_code": None},
    ])
    sync.process_sync_requests(itop, dash, dry_run=False)

    # jam dari antrian (format postgres HH:MM:SS) dinormalisasi & diteruskan ke iTop
    assert itop.calls[0][3:] == ("08:00", "17:30")

    by_req = {}
    for rid, status, stats, error in dash.updates:
        by_req.setdefault(rid, []).append((status, stats, error))

    assert [s for s, _, _ in by_req["req-1"]] == ["running", "done"]
    assert by_req["req-1"][1][1]["pushed"] == 1
    assert [s for s, _, _ in by_req["req-2"]] == ["running", "error"]
    assert "YYYY-MM-DD" in by_req["req-2"][1][2]
