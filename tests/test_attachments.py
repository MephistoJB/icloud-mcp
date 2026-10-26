"""Offline attachment contracts: real MIME parsing, strict recording IMAP fake."""

import asyncio
import base64
from email import policy
from email.message import EmailMessage

import pytest

from icloud_mcp import attachments as a, auth, email as mail, server
from icloud_mcp.config import config


def run(coro):
    return asyncio.run(coro)


class IMAP:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.size = len(raw)
        self.missing = False
        self.closed = False

    def select_folder(self, folder, readonly=False):
        assert readonly is True
        self.calls.append(("select", folder, readonly))
        return {b"UIDVALIDITY": 42}

    def fetch(self, ids, fields):
        assert ids == [17]
        self.calls.append(("fetch", ids, fields))
        if self.missing:
            return {}
        if fields == [b"RFC822.SIZE"]:
            return {17: {b"RFC822.SIZE": self.size}}
        assert fields == [f"BODY.PEEK[]<0.{a.MAX_MESSAGE_BYTES + 1}>".encode()]
        return {17: {b"BODY[]<0>": self.raw}}


@pytest.fixture(autouse=True)
def no_live_mail(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live mail is forbidden")

    monkeypatch.setattr(mail, "_get_imap_client", forbidden)
    monkeypatch.setattr(mail, "_get_smtp_client", forbidden)


@pytest.fixture
def connect(monkeypatch):
    def install(raw):
        client = IMAP(raw)
        monkeypatch.setattr(mail, "require_mail_auth", lambda _: ("offline", "fake"))
        monkeypatch.setattr(mail, "_get_imap_client", lambda *args: client)

        def close(c):
            c.closed = True

        monkeypatch.setattr(mail, "_close_imap_client", close)
        return client

    return install


def message():
    msg = EmailMessage()
    msg.set_content("body")
    msg.add_alternative("<p>body</p>", subtype="html")
    msg.add_attachment(
        b"\x00\xff\x01",
        maintype="application",
        subtype="octet-stream",
        filename="résumé.bin",
    )
    msg.add_attachment(
        b"second", maintype="application", subtype="octet-stream", filename="résumé.bin"
    )
    msg.add_attachment(
        b"image",
        maintype="image",
        subtype="png",
        filename="logo.png",
        disposition="inline",
        cid="<logo>",
    )
    return msg


def test_nested_mime_duplicates_and_exact_binary(connect):
    client = connect(message().as_bytes(policy=policy.SMTP))
    result = run(a.list_attachments(None, "17", "Archive/2026"))
    rows = result["attachments"]
    assert [r["part_id"] for r in rows] == ["1.2", "1.3"]
    assert [r["filename"] for r in rows] == ["résumé.bin"] * 2
    assert [r["size"] for r in rows] == [3, 6]
    assert rows[0]["mime_type"] == "application/octet-stream"
    assert run(a.list_attachments(None, "17", "Archive/2026")) == result
    got = run(a.get_attachment(None, "17", "1.2", "Archive/2026"))
    assert got["encoding"] == "base64"
    assert base64.b64decode(got["content_base64"], validate=True) == b"\x00\xff\x01"
    assert got["folder"] == "Archive/2026" and got["message_id"] == "17"
    assert client.closed
    assert all(call[0] in ("select", "fetch") for call in client.calls)


def test_inline_opt_in_and_targeted_retrieval(connect):
    connect(message().as_bytes())
    rows = run(a.list_attachments(None, "17", include_inline=True))["attachments"]
    assert rows[-1]["inline"] is True
    assert rows[-1]["disposition"] == "inline"
    got = run(a.get_attachment(None, "17", rows[-1]["part_id"]))
    assert base64.b64decode(got["content_base64"]) == b"image"
    with pytest.raises(a.AttachmentError, match="not an attachment"):
        run(a.get_attachment(None, "17", "1.1.1"))
    with pytest.raises(a.AttachmentError, match="not found"):
        run(a.get_attachment(None, "17", "1.99"))


@pytest.mark.parametrize(
    "header,filename",
    [
        (b'filename="=?utf-8?b?csOpc3Vtw6kudHh0?="', "résumé.txt"),
        (b"filename*=utf-8''r%C3%A9sum%C3%A9.txt", "résumé.txt"),
        (b"filename*0*=utf-8''r%C3%A9; filename*1*=sum%C3%A9.txt", "résumé.txt"),
    ],
)
def test_standard_filename_decoding(connect, header, filename):
    connect(
        b"Content-Type: application/octet-stream\r\nContent-Disposition: attachment; "
        + header
        + b"\r\n\r\nabc"
    )
    row = run(a.list_attachments(None, "17"))["attachments"][0]
    assert row["filename"] == filename


@pytest.mark.parametrize("payload", [b"", b"abc", b"\x00\xff"])
def test_empty_and_binary_single_part_attachments(connect, payload):
    connect(
        b"Content-Type: application/octet-stream\r\nContent-Disposition: attachment\r\nContent-Transfer-Encoding: binary\r\n\r\n"
        + payload
    )
    row = run(a.list_attachments(None, "17"))["attachments"][0]
    assert row["filename"] is None and row["size"] == len(payload)
    assert (
        base64.b64decode(run(a.get_attachment(None, "17", "1"))["content_base64"])
        == payload
    )


def test_attached_message_children_have_unique_paths(connect):
    inner = message()
    outer = EmailMessage()
    outer.set_content("outer")
    outer.add_attachment(inner, filename="forward.eml")
    connect(outer.as_bytes())
    rows = run(a.list_attachments(None, "17"))["attachments"]
    assert len({r["part_id"] for r in rows}) == len(rows)
    assert rows[0]["mime_type"] == "message/rfc822"
    assert rows[0]["size"] is None and not rows[0]["retrievable"]
    with pytest.raises(a.AttachmentError, match="Container"):
        run(a.get_attachment(None, "17", rows[0]["part_id"]))
    assert (
        base64.b64decode(run(a.get_attachment(None, "17", "1.2.1.2"))["content_base64"])
        == b"\x00\xff\x01"
    )


@pytest.mark.parametrize(
    "uid",
    [
        "0",
        "-1",
        "01",
        "1:20",
        "*",
        "1,2",
        "17\r\n",
        "4294967296",
        "1" * 1000,
        "１７",
        "../17",
        17,
        None,
    ],
)
def test_reject_unsafe_uids_before_connection(uid):
    with pytest.raises(a.AttachmentError, match="UID"):
        run(a.list_attachments(None, uid))


@pytest.mark.parametrize(
    "part",
    ["../x", "/tmp/file", "1] BODY[]", "1.0", "0", "1..2", "1\n", "1" * 130, "*", None],
)
def test_reject_paths_and_part_injection_before_connection(part):
    with pytest.raises(a.AttachmentError, match="part_id"):
        run(a.get_attachment(None, "17", part))


@pytest.mark.parametrize(
    "folder",
    ["", "/tmp/file", "../INBOX", "a/../b", "INBOX\r\n", "INBOX\x00", "\\tmp\\x"],
)
def test_reject_unsafe_folders_before_connection(folder):
    with pytest.raises(a.AttachmentError, match="folder"):
        run(a.list_attachments(None, "17", folder))


def test_missing_message_and_cleanup(connect):
    client = connect(b"body")
    client.missing = True
    with pytest.raises(a.AttachmentError, match="not found"):
        run(a.list_attachments(None, "17"))
    assert client.closed


@pytest.mark.parametrize("size", [None, -1, "100", True])
def test_invalid_server_size(connect, size):
    client = connect(b"body")
    client.size = size
    with pytest.raises(a.AttachmentError, match="size"):
        run(a.list_attachments(None, "17"))
    assert len(client.calls) == 2 and client.closed


def test_message_size_preflight_and_partial_response_bound(connect, monkeypatch):
    monkeypatch.setattr(a, "MAX_MESSAGE_BYTES", 10)
    client = connect(b"x" * 11)
    with pytest.raises(a.AttachmentError, match="exceeds 10"):
        run(a.list_attachments(None, "17"))
    assert len(client.calls) == 2
    client.size = 10
    with pytest.raises(a.AttachmentError, match="exceeds 10"):
        run(a.list_attachments(None, "17"))
    assert client.calls[-1][2] == [b"BODY.PEEK[]<0.11>"]
    assert client.closed


@pytest.mark.parametrize("raw,size", [(b"", 0), (b"abc", 4)])
def test_empty_or_truncated_message(connect, raw, size):
    client = connect(raw)
    client.size = size
    with pytest.raises(a.AttachmentError, match="empty, truncated"):
        run(a.list_attachments(None, "17"))


def test_missing_literal(connect):
    client = connect(b"body")
    original = client.fetch

    def fetch(ids, fields):
        return original(ids, fields) if fields == [b"RFC822.SIZE"] else {17: {}}

    client.fetch = fetch
    with pytest.raises(a.AttachmentError, match="body is missing"):
        run(a.get_attachment(None, "17", "1"))
    assert client.closed


@pytest.mark.parametrize(
    "cte,payload,error",
    [
        (b"base64", b"A!!!", "Corrupt"),
        (b"x-unknown", b"abc", "Unsupported"),
    ],
)
def test_corrupt_or_unsupported_parts_are_not_returned(connect, cte, payload, error):
    connect(
        b"Content-Disposition: attachment\r\nContent-Transfer-Encoding: "
        + cte
        + b"\r\n\r\n"
        + payload
    )
    row = run(a.list_attachments(None, "17"))["attachments"][0]
    assert not row["retrievable"] and error in row["error"]
    with pytest.raises(a.AttachmentError, match=error):
        run(a.get_attachment(None, "17", "1"))


def test_file_size_bound_and_other_part_still_retrievable(connect, monkeypatch):
    monkeypatch.setattr(a, "MAX_ATTACHMENT_BYTES", 3)
    connect(message().as_bytes())
    rows = run(a.list_attachments(None, "17"))["attachments"]
    assert rows[0]["retrievable"] and not rows[1]["retrievable"]
    assert run(a.get_attachment(None, "17", "1.2"))["size"] == 3
    with pytest.raises(a.AttachmentError, match="exceeds 3"):
        run(a.get_attachment(None, "17", "1.3"))


@pytest.mark.parametrize(
    "constant,limit,method",
    [
        ("MAX_METADATA_BYTES", 10, "list"),
        ("MAX_RESULT_BYTES", 10, "get"),
        ("MAX_ATTACHMENTS", 1, "list"),
        ("MAX_PARTS", 1, "list"),
        ("MAX_DEPTH", 1, "get"),
    ],
)
def test_result_and_tree_bounds(connect, monkeypatch, constant, limit, method):
    connect(message().as_bytes())
    monkeypatch.setattr(a, constant, limit)
    with pytest.raises(a.AttachmentError, match="exceeds|limit"):
        run(
            a.list_attachments(None, "17")
            if method == "list"
            else a.get_attachment(None, "17", "1.2")
        )


def test_malformed_multipart_fails_closed(connect):
    connect(b'Content-Type: multipart/mixed; boundary="missing"\r\n\r\nbad body')
    with pytest.raises(a.AttachmentError, match="Malformed"):
        run(a.list_attachments(None, "17"))


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        (server.email_list_attachments, {}),
        (server.email_get_attachment, {"part_id": "1.2"}),
    ],
)
def test_mcp_scope_denial_happens_before_mail_auth(monkeypatch, tool, kwargs):
    monkeypatch.setattr(auth, "get_access_token", lambda: None)
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer fake"}
    )
    monkeypatch.setattr(
        config,
        "MCP_CLIENTS",
        {"fake": {"client_id": "writer", "scopes": ["mail:write"]}},
    )
    result = run(tool(None, "17", **kwargs))
    assert result["status"] == 401


def test_mcp_read_scope_schema_annotations_and_errors(connect, monkeypatch):
    connect(message().as_bytes())
    monkeypatch.setattr(auth, "get_access_token", lambda: None)
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer fake"}
    )
    monkeypatch.setattr(
        config,
        "MCP_CLIENTS",
        {"fake": {"client_id": "reader", "scopes": ["mail:read"]}},
    )
    assert len(run(server.email_list_attachments(None, "17"))["attachments"]) == 2
    assert run(server.email_get_attachment(None, "17", "1.2"))["size"] == 3
    bad = run(server.email_get_attachment(None, "17", "../x"))
    assert bad["status"] == 400 and "part_id" in bad["error"]
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    for name in ("email_list_attachments", "email_get_attachment"):
        tool = tools[name]
        assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
        assert "folder" not in tool.parameters["required"]
    assert "part_id" in tools["email_get_attachment"].parameters["required"]


def test_real_imapclient_partial_literal_response_contract():
    from imapclient.response_parser import parse_fetch_response

    response = parse_fetch_response([(b"1 (UID 17 BODY[]<0> {3}", b"abc"), b")"])
    assert response[17][b"BODY[]<0>"] == b"abc"


def test_quoted_printable_and_content_type_name(connect):
    connect(
        b'Content-Type: application/octet-stream; name="file.bin"\r\n'
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n=00=FF=01"
    )
    row = run(a.list_attachments(None, "17"))["attachments"][0]
    assert row["filename"] == "file.bin"
    got = run(a.get_attachment(None, "17", row["part_id"]))
    assert got["content_base64"] == "AP8B" and got["size"] == 3


def test_cid_resource_without_filename_is_inline(connect):
    connect(b"Content-Type: image/png\r\nContent-ID: <logo>\r\n\r\nimage")
    assert run(a.list_attachments(None, "17"))["attachments"] == []
    rows = run(a.list_attachments(None, "17", include_inline=True))["attachments"]
    assert rows[0]["filename"] is None and rows[0]["inline"]


def test_folder_and_uid_are_not_searched_or_fallen_back(connect):
    client = connect(message().as_bytes())
    client.missing = True
    with pytest.raises(a.AttachmentError, match="not found"):
        run(a.get_attachment(None, "17", "1.2", "Other"))
    assert client.calls == [
        ("select", "Other", True),
        ("fetch", [17], [b"RFC822.SIZE"]),
    ]


def test_path_like_filename_is_metadata_only(connect, monkeypatch):
    from pathlib import Path

    msg = EmailMessage()
    msg.set_content("body")
    msg.add_attachment(
        b"bytes",
        maintype="application",
        subtype="octet-stream",
        filename="../../evil.sh",
    )
    connect(msg.as_bytes())

    def forbidden(*args, **kwargs):
        raise AssertionError("Attachment operations must not write or execute")

    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    rows = run(a.list_attachments(None, "17"))["attachments"]
    assert rows[0]["filename"] == "../../evil.sh"
    assert run(a.get_attachment(None, "17", rows[0]["part_id"]))["size"] == 5


def test_select_failure_closes_connection(connect):
    client = connect(b"body")

    def fail(*args, **kwargs):
        raise ConnectionError("offline select failure")

    client.select_folder = fail
    with pytest.raises(ConnectionError, match="offline select failure"):
        run(a.list_attachments(None, "17"))
    assert client.closed


def test_mcp_unexpected_failure_remains_generic(monkeypatch):
    monkeypatch.setattr(server, "require_scope", lambda *args: "reader")

    async def fail(*args):
        raise RuntimeError("internal diagnostic")

    monkeypatch.setattr(a, "list_attachments", fail)
    assert run(server.email_list_attachments(None, "17")) == {
        "error": "Internal error",
        "status": 500,
    }
