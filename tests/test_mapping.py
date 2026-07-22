"""Unit tests untuk mapping.py — jalankan: pytest tests/ -v"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import OrgMapping
from mapping import (
    map_magnitude,
    map_record_type,
    map_status,
    normalize_ticket,
    payload_hash,
    split_datetime,
    strip_html,
)

MAPPINGS = [
    OrgMapping(itop_org_id=27, itop_org_name="PT Bio Farma (Persero)", dashboard_code="BIOFARMA"),
    OrgMapping(itop_org_id=None, itop_org_name="Harita Kencana", dashboard_code="HK"),
]


# -----------------------------------------------------------------------------
# map_status
# -----------------------------------------------------------------------------
def test_map_status_in_progress_variants():
    for s in ("new", "assigned", "dispatched", "redispatched",
              "escalated_tto", "escalated_ttr", "waiting_for_approval", "approved"):
        assert map_status(s) == ("In Progres", True)


def test_map_status_pending_close():
    assert map_status("pending") == ("Pending", True)
    assert map_status("resolved") == ("Close", True)
    assert map_status("closed") == ("Close", True)
    assert map_status("rejected") == ("Close", True)


def test_map_status_unknown_and_case():
    assert map_status("RESOLVED") == ("Close", True)
    assert map_status("weird_status") == ("In Progres", False)
    assert map_status(None) == ("In Progres", False)
    assert map_status("") == ("In Progres", False)


# -----------------------------------------------------------------------------
# map_magnitude
# -----------------------------------------------------------------------------
def test_map_magnitude_priority():
    assert map_magnitude("1") == "Critical"
    assert map_magnitude("2") == "High"
    assert map_magnitude("3") == "Medium"
    assert map_magnitude("4") == "Minor / Low"
    assert map_magnitude(1) == "Critical"


def test_map_magnitude_fallback_urgency():
    assert map_magnitude(None, "2") == "High"
    assert map_magnitude("", "4") == "Minor / Low"
    assert map_magnitude("9", "2") == "High"      # priority invalid -> urgency
    assert map_magnitude(None, None) == "Medium"  # default


# -----------------------------------------------------------------------------
# map_record_type
# -----------------------------------------------------------------------------
def test_map_record_type():
    assert map_record_type("Incident") == "INC"
    assert map_record_type("UserRequest") == "SR"
    assert map_record_type("unknownclass") == "SR"


# -----------------------------------------------------------------------------
# split_datetime
# -----------------------------------------------------------------------------
def test_split_datetime():
    assert split_datetime("2026-07-23 10:15:42") == ("2026-07-23", "10:15")
    assert split_datetime("2026-07-23T10:15:42") == ("2026-07-23", "10:15")
    assert split_datetime("2026-07-23 10:15") == ("2026-07-23", "10:15")
    assert split_datetime(None) == (None, None)
    assert split_datetime("") == (None, None)
    assert split_datetime("bukan tanggal") == (None, None)


# -----------------------------------------------------------------------------
# strip_html
# -----------------------------------------------------------------------------
def test_strip_html():
    html = "<p>Baris satu</p><p>Baris <b>dua</b> &amp; tiga</p><br/>empat"
    out = strip_html(html)
    assert "Baris satu" in out
    assert "Baris dua & tiga" in out
    assert "<" not in out


def test_strip_html_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# -----------------------------------------------------------------------------
# normalize_ticket
# -----------------------------------------------------------------------------
def _sample_ticket(**overrides):
    fields = {
        "id": "123",
        "ref": "R-000123",
        "title": "Suspicious login from external IP",
        "description": "<p>Detail alert</p>",
        "status": "assigned",
        "operational_status": "ongoing",
        "org_id": "27",
        "org_name": "PT Bio Farma (Persero)",
        "caller_id_friendlyname": "Budi Santoso",
        "agent_id_friendlyname": "Andi Fitra",
        "service_name": "SOC Monitoring",
        "start_date": "2026-07-23 10:15:42",
        "last_update": "2026-07-23 10:20:01",
        "close_date": "",
        "resolution_date": "",
        "priority": "2",
        "urgency": "2",
        "impact": "3",
    }
    fields.update(overrides)
    return {"id": 123, "class": "UserRequest", "key": "UserRequest::123", "fields": fields}


def test_normalize_ticket_basic():
    p = normalize_ticket(_sample_ticket(), MAPPINGS)
    assert p is not None
    assert p["ticket_itop"] == "R-000123"
    assert p["record_type"] == "SR"
    assert p["source_class"] == "UserRequest"
    assert p["client_code"] == "BIOFARMA"
    assert p["event_status"] == "In Progres"
    assert p["itop_status"] == "assigned"
    assert p["magnitude"] == "High"
    assert p["ticket_date"] == "2026-07-23"
    assert p["ticket_time"] == "10:15"
    assert p["user_response_date"] is None
    assert p["note"].startswith("[iTop] Caller: Budi Santoso / Service: SOC Monitoring")
    assert "Detail alert" in p["note"]


def test_normalize_ticket_resolved_uses_resolution_date():
    p = normalize_ticket(
        _sample_ticket(status="resolved", resolution_date="2026-07-23 14:00:00"),
        MAPPINGS,
    )
    assert p["event_status"] == "Close"
    assert p["user_response_date"] == "2026-07-23"
    assert p["user_response_time"] == "14:00"


def test_normalize_ticket_org_name_fallback():
    p = normalize_ticket(
        _sample_ticket(org_id="99", org_name="harita kencana"),
        MAPPINGS,
    )
    assert p is not None
    assert p["client_code"] == "HK"


def test_normalize_ticket_unmapped_org_returns_none():
    p = normalize_ticket(
        _sample_ticket(org_id="99", org_name="PT Tidak Dikenal"),
        MAPPINGS,
    )
    assert p is None


def test_normalize_incident_record_type():
    t = _sample_ticket(ref="I-000045")
    t["class"] = "Incident"
    p = normalize_ticket(t, MAPPINGS)
    assert p["record_type"] == "INC"


def test_note_truncated_to_2000():
    p = normalize_ticket(_sample_ticket(description="x" * 5000), MAPPINGS)
    assert len(p["note"]) <= 2000


# -----------------------------------------------------------------------------
# payload_hash
# -----------------------------------------------------------------------------
def test_payload_hash_stable_and_sensitive():
    p1 = normalize_ticket(_sample_ticket(), MAPPINGS)
    p2 = normalize_ticket(_sample_ticket(), MAPPINGS)
    assert payload_hash(p1) == payload_hash(p2)

    p3 = normalize_ticket(_sample_ticket(status="pending"), MAPPINGS)
    assert payload_hash(p1) != payload_hash(p3)
