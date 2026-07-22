"""
Mapping iTop ticket → payload dashboard activity.

Semua fungsi murni (tanpa I/O) supaya gampang di-unit-test.
String hasil mapping HARUS persis sama dengan enum di
soc-dashboard/lib/constants.ts — termasuk typo 'In Progres' & 'Minor / Low'.
"""

from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from typing import Any

from config import OrgMapping, resolve_client_code

NOTE_MAX_LEN = 2000

# -----------------------------------------------------------------------------
# Status mapping — iTop status → EVENT_STATUS_OPTIONS
# -----------------------------------------------------------------------------
_STATUS_MAP: dict[str, str] = {
    "new": "In Progres",
    "assigned": "In Progres",
    "dispatched": "In Progres",
    "redispatched": "In Progres",
    "escalated_tto": "In Progres",
    "escalated_ttr": "In Progres",
    "waiting_for_approval": "In Progres",
    "approved": "In Progres",
    "pending": "Pending",
    "resolved": "Close",
    "closed": "Close",
    "rejected": "Close",
}


def map_status(itop_status: str | None) -> tuple[str, bool]:
    """Return (event_status, known). known=False → caller harus WARN log."""
    key = (itop_status or "").strip().lower()
    if key in _STATUS_MAP:
        return _STATUS_MAP[key], True
    return "In Progres", False


# -----------------------------------------------------------------------------
# Magnitude — iTop priority (fallback urgency) 1-4 → MAGNITUDE_OPTIONS
# -----------------------------------------------------------------------------
_MAGNITUDE_MAP: dict[str, str] = {
    "1": "Critical",
    "2": "High",
    "3": "Medium",
    "4": "Minor / Low",
}


def map_magnitude(priority: Any, urgency: Any = None) -> str:
    for raw in (priority, urgency):
        key = str(raw).strip() if raw not in (None, "") else ""
        if key in _MAGNITUDE_MAP:
            return _MAGNITUDE_MAP[key]
    return "Medium"


# -----------------------------------------------------------------------------
# Record type — class iTop → EVT/INC/SR
# -----------------------------------------------------------------------------
_RECORD_TYPE_MAP: dict[str, str] = {
    "incident": "INC",
    "userrequest": "SR",
}


def map_record_type(itop_class: str) -> str:
    return _RECORD_TYPE_MAP.get(itop_class.strip().lower(), "SR")


# -----------------------------------------------------------------------------
# Datetime split — 'YYYY-MM-DD HH:MM:SS' → ('YYYY-MM-DD', 'HH:MM')
# -----------------------------------------------------------------------------
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?")


def split_datetime(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    m = _DT_RE.match(raw.strip())
    if not m:
        return None, None
    return m.group(1), f"{m.group(2)}:{m.group(3)}"


# -----------------------------------------------------------------------------
# HTML strip untuk description iTop → note
# -----------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


# -----------------------------------------------------------------------------
# Normalisasi 1 ticket → payload API dashboard
# -----------------------------------------------------------------------------
def normalize_ticket(
    ticket: dict[str, Any],
    mappings: list[OrgMapping],
) -> dict[str, Any] | None:
    """
    Input: dict hasil ItopClient.get_tickets() ({"id", "class", "fields"}).
    Return payload untuk POST /api/integrations/itop, atau None kalau
    org tidak ter-mapping (caller yang WARN log).
    """
    fields = ticket.get("fields") or {}
    itop_class = ticket.get("class") or ""

    client_code = resolve_client_code(fields.get("org_id"), fields.get("org_name"), mappings)
    if client_code is None:
        return None

    event_status, _known = map_status(fields.get("status"))
    ticket_date, ticket_time = split_datetime(fields.get("start_date"))
    close_raw = fields.get("resolution_date") or fields.get("close_date")
    close_date, close_time = split_datetime(close_raw)

    note_body = strip_html(fields.get("description"))
    header_bits = []
    if fields.get("caller_id_friendlyname"):
        header_bits.append(f"Caller: {fields['caller_id_friendlyname']}")
    if fields.get("service_name"):
        header_bits.append(f"Service: {fields['service_name']}")
    if fields.get("agent_id_friendlyname"):
        header_bits.append(f"Agent: {fields['agent_id_friendlyname']}")
    header = f"[iTop] {' / '.join(header_bits)}" if header_bits else "[iTop]"
    note = f"{header}\n{note_body}" if note_body else header
    note = note[:NOTE_MAX_LEN]

    return {
        "ticket_itop": fields.get("ref") or f"{itop_class}::{ticket.get('id')}",
        "record_type": map_record_type(itop_class),
        "source_class": itop_class,
        "client_code": client_code,
        "itop_status": (fields.get("status") or "").strip().lower() or None,
        "event_status": event_status,
        "alert_name": fields.get("title") or None,
        "title": fields.get("title") or None,
        "magnitude": map_magnitude(fields.get("priority"), fields.get("urgency")),
        "ticket_date": ticket_date,
        "ticket_time": ticket_time,
        "user_response_date": close_date,
        "user_response_time": close_time,
        "note": note,
        "itop_last_update": fields.get("last_update") or None,
    }


def payload_hash(payload: dict[str, Any]) -> str:
    """Hash deterministik untuk dedup — perubahan payload apa pun mengubah hash."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
