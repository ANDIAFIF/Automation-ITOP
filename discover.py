"""
discover.py — Recon read-only ke instance iTop SEBELUM sync dijalankan.

Jalankan sekali dari PC yang bisa akses iTop (butuh .env terisi):
    .venv\Scripts\python discover.py    (Windows; Mac dev: .venv/bin/python discover.py)

Output:
  1. Operations yang tersedia untuk REST user (harus ada core/get)
  2. Per class (UserRequest, Incident): ada/tidaknya class + jumlah sample
  3. Distinct org_id/org_name yang muncul  -> bahan isi clients_map.yaml
  4. Distinct status yang muncul           -> validasi _STATUS_MAP di mapping.py
  5. Sample 1 ticket mentah                -> validasi nama field OUTPUT_FIELDS

Tidak menulis apa pun — murni core/get.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter

from itop_client import OUTPUT_FIELDS, ItopClient, ItopError
from mapping import _STATUS_MAP  # noqa: PLC2701 — sengaja introspeksi map internal

logging.basicConfig(level="WARNING")

SAMPLE_LIMIT = 50


def discover_class(client: ItopClient, itop_class: str) -> None:
    print(f"\n{'=' * 60}\nCLASS: {itop_class}\n{'=' * 60}")
    try:
        tickets = client.get_tickets(itop_class, since=None, limit=SAMPLE_LIMIT)
    except ItopError as e:
        print(f"  ❌ GAGAL: {e}")
        print("     -> Class tidak ada, atau REST user tidak punya hak read,")
        print("        atau salah satu OUTPUT_FIELDS tidak dikenal di class ini.")
        return

    print(f"  ✓ core/get OK — {len(tickets)} ticket terambil (limit {SAMPLE_LIMIT})")
    if not tickets:
        print("  (kosong — belum ada ticket di class ini)")
        return

    orgs = Counter(
        (t["fields"].get("org_id"), t["fields"].get("org_name")) for t in tickets
    )
    print("\n  Distinct org (isi ke clients_map.yaml):")
    for (org_id, org_name), n in orgs.most_common():
        print(f"    org_id={org_id!r:8} org_name={org_name!r}  ({n} ticket)")

    statuses = Counter(t["fields"].get("status") for t in tickets)
    print("\n  Distinct status (validasi _STATUS_MAP di mapping.py):")
    for status, n in statuses.most_common():
        known = (status or "").strip().lower() in _STATUS_MAP
        flag = "✓" if known else "⚠️  BELUM TER-MAPPING"
        print(f"    {flag} {status!r}  ({n} ticket)")

    print("\n  Sample ticket mentah (cek nama field):")
    sample = tickets[-1]
    print(f"    key={sample['key']}")
    print(json.dumps(sample["fields"], indent=4, ensure_ascii=False)[:1500])


def main() -> int:
    client = ItopClient.from_settings()
    print(f"Endpoint : {client.endpoint}")
    print(f"User     : {client.user}")
    print(f"Fields   : {OUTPUT_FIELDS}")

    print("\n--- list_operations ---")
    try:
        ops = client.list_operations()
    except ItopError as e:
        print(f"❌ Gagal konek/auth: {e}")
        return 1
    for op in ops:
        print(f"  - {op}")
    if "core/get" not in ops:
        print("❌ core/get TIDAK tersedia — tambahkan profile 'REST Services User'")
        return 1
    print("✓ core/get tersedia")

    for cls in ("UserRequest", "Incident"):
        discover_class(client, cls)

    print("\nSelesai. Kalau ada status ⚠️ atau field kosong/aneh di sample,")
    print("kabari untuk update mapping.py / OUTPUT_FIELDS sebelum sync jalan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
