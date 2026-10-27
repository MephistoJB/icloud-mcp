"""Bounded, read-only attachment access using the standard library MIME parser."""

import base64
import json
import re
from email import policy
from email.errors import HeaderParseError, InvalidHeaderDefect
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser, HeaderParser

from . import email as mail

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
MAX_ATTACHMENTS = 100
MAX_PARTS = 1000
MAX_DEPTH = 30


class AttachmentError(ValueError):
    """A safe, client-visible attachment validation or limit error."""


def _validate(message_id, folder, part_id=None):
    if (
        not isinstance(message_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", message_id)
        or int(message_id) > 4294967295
    ):
        raise AttachmentError("message_id must be one decimal IMAP UID (1..4294967295)")
    # Folder names are IMAP mailbox names, never filesystem paths. Slashes in
    # mailbox hierarchies are valid; control characters and traversal are not.
    if (
        not isinstance(folder, str)
        or not folder
        or len(folder) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in folder)
        or folder.startswith(("/", "\\"))
        or "\\" in folder
        or any(p in (".", "..") for p in folder.split("/"))
    ):
        raise AttachmentError("folder must be an exact safe IMAP mailbox name")
    if part_id is not None and (
        not isinstance(part_id, str)
        or len(part_id) > 128
        or not re.fullmatch(r"[1-9][0-9]{0,3}(?:\.[1-9][0-9]{0,3})*", part_id)
    ):
        raise AttachmentError(
            "part_id must be a MIME part identifier returned by listing"
        )


async def _load(context, message_id, folder):
    username, password = mail.require_mail_auth(context)
    client = await mail._run(mail._get_imap_client, username, password)
    uid = int(message_id)
    try:
        await mail._run(client.select_folder, folder, readonly=True)
        sizes = await mail._run(client.fetch, [uid], [b"RFC822.SIZE"])
        if uid not in sizes:
            raise AttachmentError("Message not found in the selected folder")
        size = sizes[uid].get(b"RFC822.SIZE")
        if type(size) is not int or size < 0:
            raise AttachmentError("Message size is missing or invalid")
        if size > MAX_MESSAGE_BYTES:
            raise AttachmentError(f"Message exceeds {MAX_MESSAGE_BYTES} encoded bytes")
        # Partial PEEK bounds the requested literal even if SIZE is inaccurate.
        response = await mail._run(
            client.fetch,
            [uid],
            [f"BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>".encode("ascii")],
        )
        raw = response.get(uid, {}).get(b"BODY[]<0>")
        if not isinstance(raw, bytes):
            raise AttachmentError("Message body is missing")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise AttachmentError(f"Message exceeds {MAX_MESSAGE_BYTES} encoded bytes")
        if len(raw) != size or not raw:
            raise AttachmentError(
                "Message body is empty, truncated, or has an inconsistent size"
            )
        try:
            return BytesParser(policy=policy.default).parsebytes(raw)
        except (ValueError, RecursionError) as exc:
            raise AttachmentError("Malformed MIME message") from exc
    finally:
        mail._close_imap_client(client)


def _parts(message):
    """Stable tree paths, independent of filenames; no hand-written MIME parser."""
    stack = [(message, "1", 0)]
    count = 0
    while stack:
        part, path, depth = stack.pop()
        count += 1
        if count > MAX_PARTS or depth > MAX_DEPTH:
            raise AttachmentError("MIME tree exceeds part count or nesting limit")
        if part.defects:
            raise AttachmentError("Malformed MIME container")
        if part.is_multipart():
            children = part.get_payload()
            for index in range(len(children), 0, -1):
                child_path = f"{path}.{index}"
                stack.append((children[index - 1], child_path, depth + 1))
        yield path, part


def _raw_parameter_header(part, name):
    raw = next(value for key, value in part.raw_items() if key.lower() == name)
    temporary = Message(policy=policy.compat32)
    temporary[name] = raw
    return temporary


def _recoverable_filename_header(part, name, header):
    """Allow only encoded-word formatting defects isolated to a filename."""
    parameter = {"content-type": "name", "content-disposition": "filename"}.get(name)
    if parameter is None or any(
        type(defect) is not InvalidHeaderDefect
        or str(defect)
        not in {
            "encoded word inside quoted string",
            "missing trailing whitespace after encoded-word",
        }
        for defect in header.defects
    ):
        return False
    try:
        # compat32 preserves other parameters' raw encoded words. Rebuilding
        # from str(header) would normalize them and hide unrelated defects.
        temporary = _raw_parameter_header(part, name)
        filename = temporary.get_param(parameter, header=name)
        if not isinstance(filename, str):
            return False
        decoded = decode_header(filename)
        if not any(charset for _, charset in decoded):
            return False
        for text, charset in decoded:
            if isinstance(text, bytes):
                text.decode(charset or "ascii", errors="strict")
        temporary.del_param(parameter, header=name)
        remaining = HeaderParser(policy=policy.default).parsestr(
            f"{name}: {temporary[name]}\n\n"
        )[name]
        semantic = "content_type" if name == "content-type" else "content_disposition"
        return not remaining.defects and getattr(remaining, semantic) == getattr(
            header, semantic
        )
    except (HeaderParseError, ValueError, LookupError):
        # Unknown encodings or future/unrecognized parser behavior fail closed.
        return False


def _headers_valid(part):
    # Parsing does not itself flag duplicate fields: never choose an ambiguous
    # first Content-Type/Disposition/Transfer-Encoding, even if each is valid.
    for name in (
        "Content-Type",
        "Content-Disposition",
        "Content-Transfer-Encoding",
        "MIME-Version",
    ):
        if len(part.get_all(name, [])) > 1:
            return False
        if name in ("Content-Type", "Content-Disposition") and name in part:
            try:
                temporary = _raw_parameter_header(part, name.lower())
                parameters = temporary.get_params([], header=name)[1:]
                names = [key.lower() for key, _ in parameters]
                if len(names) != len(set(names)):
                    return False
            except (HeaderParseError, ValueError, LookupError):
                return False
    return all(
        not getattr(header, "defects", ())
        or _recoverable_filename_header(part, name.lower(), header)
        for name, header in part.items()
    )


def _metadata(part_id, part):
    filename = part.get_filename()
    disposition = part.get_content_disposition()
    inline = disposition == "inline" or (
        disposition != "attachment" and part.get("Content-ID") is not None
    )
    if disposition != "attachment" and filename is None and not inline:
        return None
    # Do not reserialize attached message/rfc822 or multipart containers: that
    # would silently change their original bytes. Their leaf children remain
    # selectable using their own tree identifiers.
    error = None
    payload = None
    if part.is_multipart():
        error = "Container attachments cannot be retrieved; select a leaf part"
    else:
        cte = str(part.get("Content-Transfer-Encoding", "7bit")).lower().strip()
        if cte not in ("7bit", "8bit", "binary", "base64", "quoted-printable"):
            error = "Unsupported Content-Transfer-Encoding"
        else:
            payload = part.get_payload(decode=True)
            if part.defects or not _headers_valid(part):
                error = "Corrupt MIME part or headers"
            elif payload is None:
                error = "Attachment payload is missing"
            elif len(payload) > MAX_ATTACHMENT_BYTES:
                error = f"Attachment exceeds {MAX_ATTACHMENT_BYTES} decoded bytes"
    metadata = {
        "part_id": part_id,
        "filename": filename,
        "mime_type": part.get_content_type(),
        "disposition": disposition,
        "inline": inline,
        "size": len(payload) if payload is not None and not part.defects else None,
        "retrievable": error is None,
    }
    if error:
        metadata["error"] = error
    return metadata, payload


def _bounded(result, limit):
    if len(json.dumps(result, ensure_ascii=True).encode("ascii")) > limit:
        raise AttachmentError(f"Attachment result exceeds {limit} JSON bytes")
    return result


async def list_attachments(
    context, message_id: str, folder: str = "INBOX", include_inline: bool = False
) -> dict:
    """List attachment metadata for exactly one folder/UID, without marking seen."""
    _validate(message_id, folder)
    message = await _load(context, message_id, folder)
    attachments = []
    for part_id, part in _parts(message):
        item = _metadata(part_id, part)
        if item is None or (item[0]["inline"] and not include_inline):
            continue
        attachments.append(item[0])
        if len(attachments) > MAX_ATTACHMENTS:
            raise AttachmentError(
                f"Message exceeds {MAX_ATTACHMENTS} listed attachments"
            )
    return _bounded(
        {"message_id": message_id, "folder": folder, "attachments": attachments},
        MAX_METADATA_BYTES,
    )


async def get_attachment(
    context, message_id: str, part_id: str, folder: str = "INBOX"
) -> dict:
    """Return one selected leaf attachment as RFC 4648 base64, with no disk I/O."""
    if part_id is None:
        raise AttachmentError(
            "part_id must be a MIME part identifier returned by listing"
        )
    _validate(message_id, folder, part_id)
    message = await _load(context, message_id, folder)
    # Validate the entire tree before returning, including its structural limits.
    parts = list(_parts(message))
    for candidate, part in parts:
        if candidate != part_id:
            continue
        item = _metadata(candidate, part)
        if item is None:
            raise AttachmentError(
                "Selected MIME part is not an attachment or inline resource"
            )
        metadata, payload = item
        if not metadata["retrievable"]:
            raise AttachmentError(metadata["error"])
        return _bounded(
            {
                "message_id": message_id,
                "folder": folder,
                **metadata,
                "encoding": "base64",
                "content_base64": base64.b64encode(payload).decode("ascii"),
            },
            MAX_RESULT_BYTES,
        )
    raise AttachmentError("Attachment part not found in the selected message")
