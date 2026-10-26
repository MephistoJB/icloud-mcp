"""Sender selection tests: all transports are fakes, never real mail."""

import asyncio
from email import message_from_bytes
from types import SimpleNamespace

import pytest

from icloud_mcp import auth, email as mail, server
from icloud_mcp.config import config
from icloud_mcp.idempotency import run_once

LOGIN = "me@icloud.com"
ALIAS = "john@bechara.org"


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    calls = SimpleNamespace(smtp_auth=[], imap_auth=[], sent=[], copies=[])
    monkeypatch.setattr(config, "MAIL_EMAIL", LOGIN)
    monkeypatch.setattr(config, "EMAIL_SEND_FROM_ALLOWLIST", [ALIAS])
    monkeypatch.setattr(config, "EMAIL_SEND_ALLOWLIST", [])
    monkeypatch.setattr(config, "ENABLE_MAIL_SEND", True)
    monkeypatch.setattr(config, "IDEMPOTENCY_STORE_PATH", str(tmp_path / "requests.sqlite3"))
    monkeypatch.setattr(mail, "require_mail_auth", lambda _: (LOGIN, "fake-password"))
    monkeypatch.setattr(auth, "get_access_token", lambda: SimpleNamespace(client_id="sender", scopes=["mail:send"]))

    class SMTP:
        def send_message(self, msg, from_addr=None, to_addrs=None):
            calls.sent.append((msg, from_addr, to_addrs))

        def quit(self):
            pass

        def close(self):
            pass

    class IMAP:
        def append(self, folder, msg_bytes, flags=None):
            calls.copies.append(message_from_bytes(msg_bytes))

    def smtp(username, password):
        calls.smtp_auth.append((username, password))
        return SMTP()

    def imap(username, password):
        calls.imap_auth.append((username, password))
        return IMAP()

    monkeypatch.setattr(mail, "_get_smtp_client", smtp)
    monkeypatch.setattr(mail, "_get_imap_client", imap)
    return calls


@pytest.mark.parametrize("sender", [None, LOGIN, ALIAS, ALIAS.upper()])
@pytest.mark.parametrize("html", [False, True])
def test_sender_header_auth_envelope_and_sent_copy(delivery, sender, html):
    result = asyncio.run(mail.send_message(
        None, "to@example.test", "subject", "body", cc="cc@example.test",
        bcc="bcc@example.test", html=html, from_address=sender,
    ))
    assert result["status"] == "success"
    message, envelope, recipients = delivery.sent[0]
    assert message["From"] == (sender or LOGIN)
    assert envelope == LOGIN
    assert recipients == ["to@example.test", "cc@example.test", "bcc@example.test"]
    assert delivery.smtp_auth == delivery.imap_auth == [(LOGIN, "fake-password")]
    assert delivery.copies[0]["From"] == (sender or LOGIN)


@pytest.mark.parametrize("sender", [
    "", "not-an-address", "a@", "@example.test", "evil@example.test",
    "other@bechara.org", "John <john@bechara.org>", ALIAS + ",other@example.test",
    " " + ALIAS, ALIAS + " ", "john(comment)@bechara.org", "jöhn@bechara.org",
    ALIAS + "\r\nBcc: victim@example.test", ALIAS + "\n", ALIAS + "\r",
    ALIAS + "\x00", ALIAS + "\t", "john@@bechara.org", "<" + ALIAS + ">",
])
def test_invalid_or_unapproved_sender_never_connects(delivery, sender):
    with pytest.raises(ValueError):
        asyncio.run(mail.send_message(None, "to@example.test", "s", "b", from_address=sender))
    assert not delivery.smtp_auth
    assert not delivery.imap_auth


def test_empty_sender_allowlist_is_closed(delivery, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_SEND_FROM_ALLOWLIST", [])
    with pytest.raises(ValueError, match="not approved"):
        asyncio.run(mail.send_message(None, "to@example.test", "s", "b", from_address=ALIAS))
    assert not delivery.sent


def test_header_credentials_cannot_borrow_configured_account_aliases(delivery, monkeypatch):
    monkeypatch.setattr(mail, "require_mail_auth", lambda _: ("other@icloud.com", "fake-password"))
    with pytest.raises(ValueError, match="not approved"):
        asyncio.run(mail.send_message(None, "to@example.test", "s", "b", from_address=ALIAS))
    assert not delivery.smtp_auth


def test_request_id_replays_and_rejects_changed_sender(delivery):
    async def send(sender):
        return await server.email_send(None, "to@example.test", "s", "b", request_id="alias", from_address=sender)

    first = asyncio.run(send(ALIAS))
    assert first["status"] == "success"
    assert asyncio.run(send(ALIAS)) == first
    assert asyncio.run(send(LOGIN))["status"] == 500
    assert asyncio.run(send(None))["status"] == 500
    assert len(delivery.sent) == 1


def test_pre_upgrade_request_id_fingerprint_still_replays(delivery):
    cached = {"status": "success", "message": "already sent"}

    async def previous_send():
        return cached

    asyncio.run(run_once("sender", "email_send", "legacy", ["to@example.test", "s", "b", None, None, False], previous_send))
    assert asyncio.run(server.email_send(None, "to@example.test", "s", "b", request_id="legacy")) == cached
    assert asyncio.run(server.email_send(None, "to@example.test", "s", "b", request_id="legacy", from_address=None)) == cached
    assert not delivery.sent


def test_invalid_sender_does_not_consume_request_id(delivery):
    assert asyncio.run(server.email_send(None, "to@example.test", "s", "b", request_id="retry", from_address="bad"))["status"] == 500
    assert asyncio.run(server.email_send(None, "to@example.test", "s", "b", request_id="retry", from_address=ALIAS))["status"] == "success"
    assert len(delivery.sent) == 1


def test_mcp_schema_exposes_optional_sender(delivery):
    # FastMCP's list_tools API returns Tool objects; inspect the published schema.
    async def parameters():
        tools = await server.mcp.list_tools()
        return next(tool for tool in tools if tool.name == "email_send").parameters

    spec = asyncio.run(parameters())
    assert "from_address" in spec["properties"]
    assert "from_address" not in spec.get("required", [])
    assert spec["properties"]["from_address"]["default"] is None


@pytest.mark.parametrize("sender", [
    "john(comment)@bechara.org", "John<john@bechara.org>",
    ALIAS + "\r\nBcc:victim@example.test", ALIAS + "\x00", "a@",
    "john@@bechara.org", ALIAS + ",other@example.test",
])
def test_allowlist_cannot_bypass_sender_syntax_validation(delivery, monkeypatch, sender):
    monkeypatch.setattr(config, "EMAIL_SEND_FROM_ALLOWLIST", [sender.lower()])
    with pytest.raises(ValueError, match="single bare ASCII"):
        asyncio.run(mail.send_message(None, "to@example.test", "s", "b", from_address=sender))
    assert not delivery.smtp_auth


def test_different_account_can_use_its_own_login(delivery, monkeypatch):
    other = "other@icloud.com"
    monkeypatch.setattr(mail, "require_mail_auth", lambda _: (other, "fake-password"))
    assert asyncio.run(mail.send_message(None, "to@example.test", "s", "b", from_address=other))["status"] == "success"
    assert delivery.sent[0][0]["From"] == other
    assert delivery.sent[0][1] == other
