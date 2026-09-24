"""iCloud credential, egress and MCP client authorization helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from typing import Optional, Tuple
from urllib.parse import urlparse

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token, get_http_headers

from .config import config


class AuthenticationError(Exception):
    pass


class AuthorizationError(AuthenticationError, ValueError):
    pass


def _host_matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*-"):
        suffix = pattern[1:]
        return host.endswith(suffix) and len(host) > len(suffix)
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return hmac.compare_digest(host, pattern)


def require_trusted_url(url: str, base: str, kind: str) -> None:
    parsed = urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return
    base_parsed = urlparse(base)
    if "\\" in parsed.netloc or "@" in parsed.netloc:
        raise ValueError(f"{kind} has an untrusted authority")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != base_parsed.scheme or parsed.scheme != "https":
        raise ValueError(f"{kind} must use the configured HTTPS provider")
    if parsed.port not in (None, 443):
        raise ValueError(f"{kind} uses an untrusted port")
    if not any(_host_matches(host, pattern) for pattern in config.allowed_hosts_for(base)):
        raise ValueError(f"{kind} points to an untrusted host")


def _bearer_token(headers: dict[str, str]) -> Optional[str]:
    value = headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    return token.strip() if separator and scheme.lower() == "bearer" and token.strip() else None


def _audit(event: str, principal: str, scope: str, target: str = "") -> None:
    if not config.AUDIT_LOG_ENABLED:
        return
    key = (config.AUDIT_HMAC_KEY or "icloud-mcp-audit").encode()
    target_hash = hmac.new(key, target.encode(), hashlib.sha256).hexdigest()[:16] if target else None
    print(json.dumps({
        "audit": "icloud-mcp", "event": event, "principal": principal,
        "scope": scope, "target_hash": target_hash,
    }, separators=(",", ":")), file=sys.stderr)


def require_scope(context: Context, scope: str, target: str = "") -> str:
    # FastMCP validates HTTP bearer tokens before invoking a tool and exposes
    # the resulting principal through its access-token context. Depending on
    # the transport/version, the raw Authorization header may be intentionally
    # removed before tool execution, so the verified context is authoritative.
    verified = get_access_token()
    if verified is not None:
        principal = verified.client_id
        if scope not in verified.scopes:
            _audit("denied", principal, scope, target)
            raise AuthorizationError(f"Client is not permitted to use {scope}")
        _audit("authorized", principal, scope, target)
        return principal

    headers = get_http_headers()
    token = _bearer_token(headers)
    if token:
        match = next(((candidate, data) for candidate, data in config.MCP_CLIENTS.items()
                      if hmac.compare_digest(candidate, token)), None)
        if not match:
            _audit("denied", "unknown", scope, target)
            raise AuthenticationError("Invalid MCP bearer token")
        metadata = match[1]
        principal = metadata["client_id"]
        if scope not in metadata.get("scopes", []):
            _audit("denied", principal, scope, target)
            raise AuthorizationError(f"Client is not permitted to use {scope}")
        _audit("authorized", principal, scope, target)
        return principal
    if config.MCP_CLIENTS:
        raise AuthenticationError("MCP bearer token required")
    if config.MCP_TRUST_STDIO:
        _audit("authorized", "stdio", scope, target)
        return "stdio"
    raise AuthenticationError("MCP bearer token required")


def require_enabled(enabled: bool, capability: str) -> None:
    if not enabled:
        raise AuthorizationError(f"{capability} is disabled by server policy")


def require_recipient_allowed(address: str) -> None:
    cleaned = address.strip().lower()
    if config.EMAIL_SEND_ALLOWLIST and cleaned not in config.EMAIL_SEND_ALLOWLIST:
        raise AuthorizationError("Recipient is not permitted by server policy")


def get_credentials(context: Context) -> Tuple[str, str]:
    headers = get_http_headers()
    header_present = "x-apple-email" in headers or "x-apple-app-specific-password" in headers
    if header_present:
        if not config.ALLOW_HEADER_CREDENTIALS:
            raise AuthenticationError("Per-request iCloud credentials are disabled")
        email = headers.get("x-apple-email")
        password = headers.get("x-apple-app-specific-password")
    else:
        email = config.FALLBACK_EMAIL
        password = config.FALLBACK_PASSWORD
    if not email or not password:
        raise AuthenticationError("iCloud credentials are not configured")
    return email, password


def require_auth(context: Context) -> Tuple[str, str]:
    return get_credentials(context)
