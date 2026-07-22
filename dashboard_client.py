"""
Client API dashboard — push batch ticket ternormalisasi.

POST {DASHBOARD_BASE_URL}/api/integrations/itop
  headers: X-API-Key, Content-Type: application/json
  body   : {"overwrite_mode": "full"|"partial", "tickets": [...]}

Response 200: {"results": [{"ticket_itop", "action", "error"}]}
Retry 3x exponential backoff (2/4/8s) untuk network error / 5xx.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from config import settings

log = logging.getLogger(__name__)

MAX_RETRY = 3
BACKOFF_BASE = 2  # detik → 2, 4, 8


class DashboardError(Exception):
    pass


class DashboardClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    @classmethod
    def from_settings(cls) -> "DashboardClient":
        return cls(
            base_url=settings.dashboard_base_url,
            api_key=settings.dashboard_api_key,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/api/integrations/itop"

    def push(
        self, tickets: list[dict[str, Any]], overwrite_mode: str = "full"
    ) -> list[dict[str, Any]]:
        """
        Push batch. Return list results per ticket.
        Raise DashboardError kalau gagal total (setelah retry) atau
        ada result dengan error != null — caller TIDAK boleh advance HWM.
        """
        if not self.base_url or not self.api_key:
            raise DashboardError(
                "Dashboard config kosong (cek DASHBOARD_BASE_URL/DASHBOARD_API_KEY di .env)"
            )
        if not tickets:
            return []

        body = {"overwrite_mode": overwrite_mode, "tickets": tickets}
        last_err: Exception | None = None

        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = self._session.post(
                    self.endpoint,
                    json=body,
                    headers={"X-API-Key": self.api_key},
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = e
                log.warning("Dashboard POST attempt %d/%d gagal: %s", attempt, MAX_RETRY, e)
                if attempt < MAX_RETRY:
                    time.sleep(BACKOFF_BASE ** attempt)
                continue

            # 4xx = kesalahan konfigurasi/payload — retry tidak akan menolong
            if resp.status_code in (400, 401, 403):
                raise DashboardError(
                    f"Dashboard HTTP {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code != 200:
                last_err = DashboardError(
                    f"Dashboard HTTP {resp.status_code}: {resp.text[:300]}"
                )
                log.warning("Dashboard POST attempt %d/%d: %s", attempt, MAX_RETRY, last_err)
                if attempt < MAX_RETRY:
                    time.sleep(BACKOFF_BASE ** attempt)
                continue

            try:
                data = resp.json()
            except ValueError as e:
                raise DashboardError(f"Dashboard response bukan JSON: {resp.text[:300]}") from e

            results = data.get("results") or []
            errored = [r for r in results if r.get("error")]
            if errored:
                for r in errored:
                    log.error(
                        "Ticket %s gagal di dashboard: %s", r.get("ticket_itop"), r.get("error")
                    )
                raise DashboardError(f"{len(errored)}/{len(results)} ticket gagal di dashboard")
            return results

        raise DashboardError(f"Dashboard POST gagal setelah {MAX_RETRY}x retry: {last_err}")
