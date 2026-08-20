"""Configuration and persistence.

Design notes:
  - Single-tenant. One instance = one operation's data. No accounts, no
    per-user partitioning. A forker who needs multi-user can extend the
    auth dependency in one place.
  - Data lives in plain JSON on disk, so it survives restarts, is readable,
    and can be backed up by copying a folder.
  - The instance refuses to serve anything but the setup screen until a
    password is set, so a fresh cloud deploy is never briefly wide open.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("ROUTEFORGE_DATA", "./data")).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
PLAN_PATH = DATA_DIR / "plan.json"
CACHE_PATH = DATA_DIR / "matrix_cache.json"


class InstanceConfig(BaseModel):
    """Server-side settings, written by the setup screen."""
    configured: bool = False

    # --- auth ---
    password_hash: str = ""
    password_salt: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    api_token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))

    # --- routing provider ---
    routing_provider: str = "locationiq"
    routing_api_key: str = ""
    routing_base_url: str = ""
    nominatim_url: str = ""

    # --- branding / vocabulary ---
    organization_name: str = "RouteForge"

    # --- optional SMS dispatch ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    def public_dict(self) -> Dict[str, Any]:
        """Everything safe to send to the browser — no secrets."""
        return {
            "configured": self.configured,
            "organization_name": self.organization_name,
            "routing_provider": self.routing_provider,
            "has_routing_key": bool(self.routing_api_key) or self.routing_provider == "osrm",
            "sms_enabled": bool(self.twilio_account_sid and self.twilio_auth_token),
        }


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def load_config() -> InstanceConfig:
    """Read config from disk, seeding from environment variables if present.

    Env vars let a Docker/Railway deploy come up pre-configured without the
    setup screen — useful for automated redeploys.
    """
    cfg: InstanceConfig
    if CONFIG_PATH.exists():
        try:
            cfg = InstanceConfig(**json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError, ValueError):
            cfg = InstanceConfig()
    else:
        cfg = InstanceConfig()

    # Environment overrides (only fill blanks; never clobber a saved value).
    env_map = {
        "ROUTING_PROVIDER": "routing_provider",
        "ROUTING_API_KEY": "routing_api_key",
        "LOCATIONIQ_API_KEY": "routing_api_key",
        "ORS_API_KEY": "routing_api_key",
        "OSRM_URL": "routing_base_url",
        "NOMINATIM_URL": "nominatim_url",
        "TWILIO_ACCOUNT_SID": "twilio_account_sid",
        "TWILIO_AUTH_TOKEN": "twilio_auth_token",
        "TWILIO_FROM_NUMBER": "twilio_from_number",
        "ROUTEFORGE_ORG_NAME": "organization_name",
    }
    changed = False
    for env_key, field_name in env_map.items():
        value = os.environ.get(env_key)
        if value and not getattr(cfg, field_name):
            setattr(cfg, field_name, value)
            changed = True

    # An admin password supplied by env completes setup headlessly.
    env_password = os.environ.get("ROUTEFORGE_PASSWORD")
    if env_password and not cfg.password_hash:
        set_password(cfg, env_password)
        cfg.configured = True
        changed = True

    env_token = os.environ.get("ROUTEFORGE_API_TOKEN")
    if env_token:
        cfg.api_token = env_token

    if changed or not CONFIG_PATH.exists():
        save_config(cfg)
    return cfg


def save_config(cfg: InstanceConfig) -> None:
    _atomic_write(CONFIG_PATH, cfg.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2 — stdlib only, no extra dependency)
# ---------------------------------------------------------------------------
import hashlib

PBKDF2_ROUNDS = 240_000


def set_password(cfg: InstanceConfig, password: str) -> None:
    salt = secrets.token_hex(16)
    cfg.password_salt = salt
    cfg.password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS
    ).hex()


def verify_password(cfg: InstanceConfig, password: str) -> bool:
    if not cfg.password_hash:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), cfg.password_salt.encode(), PBKDF2_ROUNDS
    ).hex()
    return secrets.compare_digest(candidate, cfg.password_hash)


# ---------------------------------------------------------------------------
# Plan storage — the working dataset (locations, vehicles, stops, ...)
# ---------------------------------------------------------------------------
DEFAULT_PLAN: Dict[str, Any] = {
    "commodities": [],
    "locations": [],
    "vehicles": [],
    "stops": [],
    "depots": [],
    "drivers": [],
    "settings": {},
}


def load_plan() -> Dict[str, Any]:
    if PLAN_PATH.exists():
        try:
            data = json.loads(PLAN_PATH.read_text())
            merged = dict(DEFAULT_PLAN)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_PLAN)


def save_plan(plan: Dict[str, Any]) -> None:
    _atomic_write(PLAN_PATH, json.dumps(plan, indent=2))
