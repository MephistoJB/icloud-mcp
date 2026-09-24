import asyncio
from types import SimpleNamespace

import pytest

from icloud_mcp import auth
from icloud_mcp.auth import AuthenticationError, AuthorizationError, require_scope
from icloud_mcp.config import config
from icloud_mcp.idempotency import run_once
from icloud_mcp.resource_ids import decode_resource_id, encode_resource_id


def test_scoped_client_allows_only_its_scope(monkeypatch):
    monkeypatch.setattr(auth, "get_access_token", lambda: None)
    monkeypatch.setattr(
        config, "MCP_CLIENTS",
        {"secret": {"client_id": "reader", "scopes": ["mail:read"]}},
    )
    monkeypatch.setattr(auth, "get_http_headers", lambda: {"authorization": "Bearer secret"})
    assert require_scope(None, "mail:read") == "reader"
    with pytest.raises(AuthorizationError):
        require_scope(None, "mail:send")


def test_missing_token_is_denied_when_clients_exist(monkeypatch):
    monkeypatch.setattr(auth, "get_access_token", lambda: None)
    monkeypatch.setattr(
        config, "MCP_CLIENTS",
        {"secret": {"client_id": "reader", "scopes": ["mail:read"]}},
    )
    monkeypatch.setattr(auth, "get_http_headers", lambda: {})
    with pytest.raises(AuthenticationError):
        require_scope(None, "mail:read")


def test_verified_fastmcp_principal_does_not_require_raw_header(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_access_token",
        lambda: SimpleNamespace(client_id="reader", scopes=["mail:read"]),
    )
    monkeypatch.setattr(auth, "get_http_headers", dict)
    assert require_scope(None, "mail:read") == "reader"
    with pytest.raises(AuthorizationError):
        require_scope(None, "mail:send")


def test_http_configuration_fails_closed_without_token(monkeypatch):
    monkeypatch.setattr(config, "MCP_CLIENTS", {})
    monkeypatch.setattr(config, "MCP_REQUIRE_AUTH", True)
    monkeypatch.setattr(config, "MCP_ALLOW_UNAUTHENTICATED_HTTP", False)
    monkeypatch.setattr(config, "MCP_BIND_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError):
        config.validate_http()


def test_signed_resource_id_round_trip_and_tamper(monkeypatch):
    monkeypatch.setattr(config, "RESOURCE_ID_SIGNING_KEY", "test-key")
    original = "https://p72-caldav.icloud.com/user/calendar/item.ics"
    encoded = encode_resource_id(original)
    assert encoded.startswith("icloud:v1:")
    assert original not in encoded
    assert decode_resource_id(encoded) == original
    prefix, signature = encoded.rsplit(".", 1)
    tampered = prefix + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(ValueError):
        decode_resource_id(tampered)


def test_legacy_resource_ids_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "RESOURCE_ID_SIGNING_KEY", "test-key")
    monkeypatch.setattr(config, "ALLOW_LEGACY_URL_IDS", False)
    with pytest.raises(ValueError):
        decode_resource_id("https://caldav.icloud.com/item.ics")


def test_idempotency_replays_without_repeating_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IDEMPOTENCY_STORE_PATH", str(tmp_path / "requests.sqlite3"))
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return {"status": "success", "sequence": calls}

    first = asyncio.run(run_once("agent", "send", "abc", {"to": "a@b.test"}, operation))
    second = asyncio.run(run_once("agent", "send", "abc", {"to": "a@b.test"}, operation))
    assert first == second
    assert calls == 1


def test_idempotency_rejects_key_reuse_with_different_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IDEMPOTENCY_STORE_PATH", str(tmp_path / "requests.sqlite3"))

    async def operation():
        return {"ok": True}

    asyncio.run(run_once("agent", "send", "abc", {"to": "a@b.test"}, operation))
    with pytest.raises(ValueError):
        asyncio.run(run_once("agent", "send", "abc", {"to": "other@b.test"}, operation))
