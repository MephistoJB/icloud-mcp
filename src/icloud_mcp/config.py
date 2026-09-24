"""Environment-only configuration for the iCloud MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))


def _secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read NAME or NAME_FILE without logging the value."""
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read {name}_FILE") from exc
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _csv(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


ALL_SCOPES = {
    "calendar:read", "calendar:write", "calendar:delete",
    "contacts:read", "contacts:write", "contacts:delete",
    "mail:read", "mail:write", "mail:delete", "mail:send",
}


def _load_clients() -> dict[str, dict[str, Any]]:
    """Load token metadata from JSON and the legacy compatibility token."""
    raw = _secret("MCP_CLIENTS_JSON")
    clients: list[dict[str, Any]] = []
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for client_id, value in parsed.items():
                if isinstance(value, str):
                    clients.append({"id": client_id, "token": value, "scopes": sorted(ALL_SCOPES)})
                else:
                    clients.append({"id": client_id, **value})
        elif isinstance(parsed, list):
            clients = parsed
        else:
            raise ValueError("MCP_CLIENTS_JSON must be an object or array")

    legacy = _secret("MCP_AUTH_TOKEN")
    if legacy:
        clients.append({"id": "legacy", "token": legacy, "scopes": sorted(ALL_SCOPES)})

    result: dict[str, dict[str, Any]] = {}
    for entry in clients:
        token = str(entry.get("token", "")).strip()
        client_id = str(entry.get("id", "")).strip()
        scopes = set(entry.get("scopes", []))
        unknown = scopes - ALL_SCOPES
        if not token or not client_id:
            raise ValueError("Every MCP client requires a non-empty id and token")
        if unknown:
            raise ValueError(f"Unknown scopes for MCP client {client_id}: {sorted(unknown)}")
        if token in result:
            raise ValueError("Duplicate MCP client token")
        result[token] = {"client_id": client_id, "scopes": sorted(scopes)}
    return result


class Config:
    CALDAV_SERVER = os.getenv("CALDAV_SERVER", "https://caldav.icloud.com").rstrip("/")
    CARDDAV_SERVER = os.getenv("CARDDAV_SERVER", "https://contacts.icloud.com").rstrip("/")
    IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.mail.me.com")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.mail.me.com")

    MCP_SERVER_PORT = int(os.getenv("MCP_SERVER_PORT", os.getenv("PORT", "8000")))
    IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SENT_FOLDER = os.getenv("SENT_FOLDER", "Sent Messages")
    HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
    MCP_BIND_HOST = os.getenv("HOST", "127.0.0.1")

    MCP_CLIENTS = _load_clients()
    MCP_REQUIRE_AUTH = _bool("MCP_REQUIRE_AUTH", True)
    MCP_ALLOW_UNAUTHENTICATED_HTTP = _bool("MCP_ALLOW_UNAUTHENTICATED_HTTP", False)
    MCP_TRUST_STDIO = _bool("MCP_TRUST_STDIO", True)

    FALLBACK_EMAIL = _secret("ICLOUD_EMAIL")
    FALLBACK_PASSWORD = _secret("ICLOUD_APP_SPECIFIC_PASSWORD")
    ALLOW_HEADER_CREDENTIALS = _bool("ICLOUD_ALLOW_HEADER_CREDENTIALS", False)

    EMAIL_SEND_ALLOWLIST = [x.lower() for x in _csv("EMAIL_SEND_ALLOWLIST")]
    ENABLE_CALENDAR_WRITE = _bool("ENABLE_CALENDAR_WRITE", True)
    ENABLE_CALENDAR_DELETE = _bool("ENABLE_CALENDAR_DELETE", False)
    ENABLE_CALENDAR_INVITATIONS = _bool("ENABLE_CALENDAR_INVITATIONS", False)
    ENABLE_CONTACTS_WRITE = _bool("ENABLE_CONTACTS_WRITE", True)
    ENABLE_CONTACTS_DELETE = _bool("ENABLE_CONTACTS_DELETE", False)
    ENABLE_MAIL_WRITE = _bool("ENABLE_MAIL_WRITE", True)
    ENABLE_MAIL_DELETE = _bool("ENABLE_MAIL_DELETE", False)
    ENABLE_PERMANENT_MAIL_DELETE = _bool("ENABLE_PERMANENT_MAIL_DELETE", False)
    ENABLE_MAIL_SEND = _bool("ENABLE_MAIL_SEND", True)

    CALDAV_ALLOWED_HOSTS = _csv("CALDAV_ALLOWED_HOSTS", "caldav.icloud.com,*-caldav.icloud.com")
    CARDDAV_ALLOWED_HOSTS = _csv("CARDDAV_ALLOWED_HOSTS", "contacts.icloud.com,*-contacts.icloud.com")
    RESOURCE_ID_SIGNING_KEY = _secret("RESOURCE_ID_SIGNING_KEY")
    ALLOW_LEGACY_URL_IDS = _bool("ALLOW_LEGACY_URL_IDS", True)

    AUDIT_LOG_ENABLED = _bool("AUDIT_LOG_ENABLED", True)
    AUDIT_HMAC_KEY = _secret("AUDIT_HMAC_KEY")
    IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400"))
    IDEMPOTENCY_STORE_PATH = os.getenv(
        "IDEMPOTENCY_STORE_PATH", "/tmp/icloud-mcp-idempotency.sqlite3"
    )

    def allowed_hosts_for(self, base: str) -> list[str]:
        base_host = (urlparse(base).hostname or "").lower().rstrip(".")
        configured = self.CALDAV_ALLOWED_HOSTS if base == self.CALDAV_SERVER else self.CARDDAV_ALLOWED_HOSTS
        return list(dict.fromkeys([base_host, *[x.lower().rstrip(".") for x in configured]]))

    def validate_http(self) -> None:
        if self.MCP_REQUIRE_AUTH and not self.MCP_CLIENTS and not self.MCP_ALLOW_UNAUTHENTICATED_HTTP:
            raise RuntimeError(
                "HTTP transport requires MCP_AUTH_TOKEN or MCP_CLIENTS_JSON(_FILE); "
                "set MCP_ALLOW_UNAUTHENTICATED_HTTP=true only for an isolated loopback deployment"
            )
        if not self.MCP_CLIENTS and self.MCP_BIND_HOST not in {"127.0.0.1", "::1", "localhost"}:
            if not self.MCP_ALLOW_UNAUTHENTICATED_HTTP:
                raise RuntimeError("Refusing unauthenticated HTTP on a non-loopback bind address")


config = Config()
