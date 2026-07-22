"""
Config loader iTop → Dashboard sync agent.

Membaca:
- `.env` (via python-dotenv) untuk secrets & runtime params
- `clients_map.yaml` untuk mapping org iTop → clients.code dashboard

Akses:
    from config import settings, get_org_mappings, resolve_client_code
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CLIENTS_MAP_PATH = BASE_DIR / "clients_map.yaml"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Environment variable wajib: {key} (cek .env)")
    return val if val is not None else ""


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Env {key} harus integer, dapat: {raw!r}") from e


@dataclass(frozen=True)
class Settings:
    # iTop
    itop_base_url: str
    itop_user: str
    itop_password: str
    itop_api_version: str

    # Dashboard
    dashboard_base_url: str
    dashboard_api_key: str

    # Runtime
    poll_interval_seconds: int
    batch_limit: int
    overwrite_mode: str          # 'full' | 'partial'
    database_path: str

    # Logging
    log_level: str
    log_file: str


def _load_settings() -> Settings:
    overwrite_mode = _env("OVERWRITE_MODE", "full").strip().lower()
    if overwrite_mode not in ("full", "partial"):
        raise RuntimeError(f"OVERWRITE_MODE harus 'full' atau 'partial', dapat: {overwrite_mode!r}")
    return Settings(
        itop_base_url=_env("ITOP_BASE_URL", ""),
        itop_user=_env("ITOP_USER", ""),
        itop_password=_env("ITOP_PASSWORD", ""),
        itop_api_version=_env("ITOP_API_VERSION", "1.3"),
        dashboard_base_url=_env("DASHBOARD_BASE_URL", ""),
        dashboard_api_key=_env("DASHBOARD_API_KEY", ""),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 60),
        batch_limit=_env_int("BATCH_LIMIT", 100),
        overwrite_mode=overwrite_mode,
        database_path=_env("DATABASE_PATH", "sync_state.db"),
        log_level=_env("LOG_LEVEL", "INFO"),
        log_file=_env("LOG_FILE", "logs/itop_sync.log"),
    )


settings = _load_settings()


# -----------------------------------------------------------------------------
# clients_map.yaml — mapping org iTop → dashboard clients.code
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class OrgMapping:
    itop_org_id: int | None
    itop_org_name: str | None
    dashboard_code: str


def _parse_mapping(raw: dict) -> OrgMapping:
    code = raw.get("dashboard_code")
    if not code:
        raise RuntimeError(f"clients_map.yaml: 'dashboard_code' wajib di entry {raw!r}")
    org_id = raw.get("itop_org_id")
    org_name = raw.get("itop_org_name")
    if org_id in (None, "") and not org_name:
        raise RuntimeError(
            f"clients_map.yaml: minimal salah satu itop_org_id / itop_org_name di entry {raw!r}"
        )
    return OrgMapping(
        itop_org_id=int(org_id) if org_id not in (None, "") else None,
        itop_org_name=str(org_name) if org_name else None,
        dashboard_code=str(code),
    )


_MAPPINGS_CACHE: list[OrgMapping] | None = None


def get_org_mappings() -> list[OrgMapping]:
    global _MAPPINGS_CACHE
    if _MAPPINGS_CACHE is None:
        if not CLIENTS_MAP_PATH.exists():
            raise RuntimeError(f"clients_map.yaml tidak ditemukan di {CLIENTS_MAP_PATH}")
        with CLIENTS_MAP_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw_list = data.get("orgs") or []
        if not isinstance(raw_list, list):
            raise RuntimeError("clients_map.yaml: 'orgs' harus berupa list")
        _MAPPINGS_CACHE = [_parse_mapping(item) for item in raw_list]
    return _MAPPINGS_CACHE


def resolve_client_code(
    org_id: int | str | None,
    org_name: str | None,
    mappings: list[OrgMapping] | None = None,
) -> str | None:
    """Match by org_id dulu (stabil), fallback org_name (case-insensitive)."""
    if mappings is None:
        mappings = get_org_mappings()
    if org_id not in (None, ""):
        try:
            oid = int(org_id)
        except (TypeError, ValueError):
            oid = None
        if oid is not None:
            for m in mappings:
                if m.itop_org_id == oid:
                    return m.dashboard_code
    if org_name:
        needle = org_name.strip().lower()
        for m in mappings:
            if m.itop_org_name and m.itop_org_name.strip().lower() == needle:
                return m.dashboard_code
    return None


if __name__ == "__main__":
    print("=== iTop Sync Config ===")
    print(f"BASE_DIR        : {BASE_DIR}")
    print(f"iTop URL        : {settings.itop_base_url}")
    print(f"Dashboard URL   : {settings.dashboard_base_url}")
    print(f"Poll interval   : {settings.poll_interval_seconds}s")
    print(f"Batch limit     : {settings.batch_limit}")
    print(f"Overwrite mode  : {settings.overwrite_mode}")
    print(f"Database        : {settings.database_path}")
    print()
    print("=== Org mappings ===")
    for m in get_org_mappings():
        print(f"  org_id={m.itop_org_id} org_name={m.itop_org_name!r} -> {m.dashboard_code}")
