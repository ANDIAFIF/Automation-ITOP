"""
iTop ITSM REST client — read-only (core/get) untuk sync agent.

Quirks penting (sama dengan vsoc/itop_client.py):
- URL WAJIB full: http(s)://<host>/webservices/rest.php?version=1.3
- Auth: form data (auth_user, auth_pwd, json_data) — BUKAN HTTP Basic.
- Sukses = response field `code == 0`.

Client ini SENGAJA tidak punya core/create / core/update — sync satu arah,
akun REST-nya pun efektif read-only dari sisi kode.

Public API:
    client = ItopClient.from_settings()
    tickets = client.get_tickets("UserRequest", since="2026-07-23 10:00:00", limit=100)
    # tickets = [{"id": 123, "class": "UserRequest", "fields": {...}}, ...]
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from config import settings

log = logging.getLogger(__name__)

# Field yang diminta per ticket. Semua ada di UserRequest & Incident.
OUTPUT_FIELDS = (
    "id, ref, title, description, status, operational_status, "
    "org_id, org_name, caller_id_friendlyname, agent_id_friendlyname, "
    "service_name, start_date, last_update, close_date, resolution_date, "
    "priority, urgency, impact"
)


class ItopError(Exception):
    pass


class ItopClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        api_version: str = "1.3",
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.api_version = api_version
        self.timeout = timeout
        self._session = requests.Session()

    @classmethod
    def from_settings(cls) -> "ItopClient":
        return cls(
            base_url=settings.itop_base_url,
            user=settings.itop_user,
            password=settings.itop_password,
            api_version=settings.itop_api_version,
        )

    @property
    def endpoint(self) -> str:
        # WAJIB: version di query string, BUKAN payload
        return f"{self.base_url}/webservices/rest.php?version={self.api_version}"

    # ------------------------------------------------------------------
    # Low-level call
    # ------------------------------------------------------------------
    def _call(self, json_data: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.user:
            raise ItopError("iTop config kosong (cek ITOP_BASE_URL/ITOP_USER di .env)")

        form = {
            "auth_user": self.user,
            "auth_pwd": self.password,
            "json_data": json.dumps(json_data),
        }
        resp = self._session.post(self.endpoint, data=form, timeout=self.timeout)
        if resp.status_code != 200:
            raise ItopError(f"iTop HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise ItopError(f"iTop response bukan JSON: {resp.text[:200]}") from e

        code = data.get("code")
        if code != 0:
            raise ItopError(f"iTop error code={code}: {data.get('message')}")
        return data

    # ------------------------------------------------------------------
    # list_operations — untuk verifikasi akses REST user
    # ------------------------------------------------------------------
    def list_operations(self) -> list[str]:
        data = self._call({"operation": "list_operations"})
        ops = data.get("operations") or []
        return [o.get("verb", "") for o in ops if isinstance(o, dict)]

    # ------------------------------------------------------------------
    # get_tickets — delta fetch per class by last_update
    # ------------------------------------------------------------------
    def get_tickets(
        self,
        itop_class: str,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch ticket dari 1 class (UserRequest / Incident).
        `since` = string datetime iTop 'YYYY-MM-DD HH:MM:SS' — inklusif (>=)
        supaya update di detik yang sama tidak hilang (dedup by payload hash
        di layer atas). None = ambil semua (dipakai backfill).
        """
        if since:
            # OQL: quote pakai single-quote, nilai kita bentuk sendiri (bukan input user)
            oql = f"SELECT {itop_class} WHERE last_update >= '{since}'"
        else:
            oql = f"SELECT {itop_class}"

        payload = {
            "operation": "core/get",
            "class": itop_class,
            "key": oql,
            "output_fields": OUTPUT_FIELDS,
            "limit": limit,
        }
        data = self._call(payload)
        objects = data.get("objects") or {}

        tickets: list[dict[str, Any]] = []
        # format key: "UserRequest::123"
        for obj_key, obj in objects.items():
            fields = obj.get("fields") or {}
            try:
                obj_id = int(obj.get("key") or fields.get("id") or 0)
            except (TypeError, ValueError):
                obj_id = 0
            tickets.append({"id": obj_id, "class": itop_class, "key": obj_key, "fields": fields})

        # urutkan by last_update supaya HWM maju konsisten
        tickets.sort(key=lambda t: t["fields"].get("last_update") or "")
        return tickets


# -----------------------------------------------------------------------------
# CLI debug: cek koneksi + list operations
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = ItopClient.from_settings()
    print(f"Endpoint: {client.endpoint}")
    ops = client.list_operations()
    print(f"Operations tersedia: {ops}")
    if "core/get" not in ops:
        print("⚠️  core/get TIDAK tersedia — cek profile REST user di iTop")
    else:
        print("✓ core/get tersedia")
