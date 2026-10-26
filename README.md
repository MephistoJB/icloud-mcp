# iCloud MCP Server

A hardened, self-hosted MCP server for iCloud Calendar (CalDAV), Contacts
(CardDAV), and Mail (IMAP/SMTP). It keeps the upstream tool names and remains
compatible with clients written for `mike-tih/icloud-mcp`, while adding
network-safe authentication, per-agent permissions, action policy, audit
events, idempotency, signed resource IDs, and an Unraid template.

## Tools

- Calendar: `calendar_list_calendars`, `calendar_list_events`,
  `calendar_search_events`, `calendar_create_event`,
  `calendar_update_event`, `calendar_delete_event`
- Contacts: `contacts_list`, `contacts_get`, `contacts_search`, `contacts_birthdays`,
  `contacts_create`, `contacts_update`, `contacts_delete`
- Mail: `email_list_folders`, `email_list_messages`, `email_get_message`,
  `email_get_messages`, `email_list_attachments`, `email_get_attachment`,
  `email_search`, `email_send`, `email_move`,
  `email_delete`, `email_mark_read`, `email_mark_unread`

`calendar_create_event`, `contacts_create`, and `email_send` accept an
optional `request_id`. Repeating the same request ID and arguments returns the
stored result without repeating the external action.

`contacts_list`, `contacts_get`, and `contacts_search` include a read-only
`birthday` field from the contact's vCard `BDAY` property. It is a string in
the stored format (for example, `1985-04-12` or `--0412` without a year), or
`null` when absent or empty. No birth year is inferred. Access uses the
existing `contacts:read` scope.

Use `contacts_birthdays(month=10, day=20)` to find everyone born on October 20,
regardless of year. It returns only `id`, `name` and the stored `birthday`,
sorted by name, or an empty list when nobody matches. Complete dates,
yearless dates and dates with times are supported; missing, invalid or
free-text birthdays are skipped. February 29 is supported; invalid query
dates such as April 31 return status 400. The tool uses `contacts:read` and
the configured contacts account. It loads fresh contacts on every call and
filters on the server, so only matches are transmitted to the MCP client.

## Read-only mail attachments

Both new tools use the existing `mail:read` scope and configured mail account.
They select exactly the supplied IMAP folder with `readonly=True` and fetch
with `BODY.PEEK`, without setting Seen, changing flags, or writing files.
No attachment is executed, rendered, uploaded, or interpreted as a local path.

1. Call `email_list_attachments(message_id="17", folder="INBOX")`.
   The result is `{message_id, folder, attachments: [...]}`. Each entry has
   `part_id`, decoded `filename` (or `null`), `mime_type`, decoded byte `size`
   (or `null` when unavailable/corrupt), `disposition`, `inline`, and
   `retrievable`. Unavailable entries also have an explanatory `error`.
2. Call `email_get_attachment(message_id="17", folder="INBOX", part_id="1.2")`
   with the exact listed identifier. The result contains the same metadata,
   `message_id`, `folder`, `encoding: "base64"`, and `content_base64`.
   `content_base64` is standard RFC 4648 ASCII base64 with padding and no line
   breaks, representing the original transfer-decoded leaf payload bytes.
   For example, bytes `00 ff 01` are returned as `"AP8B"`. Decode once in the
   MCP client to recover the binary; the server returns JSON, not a download
   URL, filesystem path, data URI, or MCP resource. A zero-byte file returns
   `size: 0` and `content_base64: ""`.

`message_id` is one canonical decimal IMAP UID, never an RFC Message-ID or UID
range. `folder` is an exact IMAP mailbox name; nested names such as
`Archive/2026` are supported, while controls and traversal components are
rejected. `part_id` is a MIME **tree path**, not an IMAP BODY section or a
filename: root is `1`, children are `1.1`, `1.2`, and so on, including
encapsulated messages. IDs are deterministic for an unchanged message in its
folder and distinguish duplicate filenames. They have the same folder/UID
lifetime as existing message tools: refresh IDs after moves or an IMAP
UIDVALIDITY reset. No filename is accepted as a retrieval selector. Returned
filenames are untrusted display metadata, including any path-like text.

By default the list includes explicit `attachment` parts and filename-bearing
parts without inline disposition or Content-ID. Pass `include_inline=true`
to also list inline/CID resources; ordinary text/HTML body parts are excluded.
Inline resources can be retrieved explicitly by their listed ID. Python's
standard `email` parser decodes MIME structure, RFC 2047/RFC 2231 filenames,
and transfer encodings. Parser-detected corrupt parts are listed as
unretrievable; structural MIME corruption fails the request. The parser is
permissive for encodings such as quoted-printable, so this is not a file
integrity or malware check. Attached `message/rfc822` and multipart containers
are listed as unretrievable (`size: null`), avoiding byte-changing
reserialization; contained file attachments remain separately selectable.

Limits are fixed and need no configuration: 16 MiB encoded message, 4 MiB
transfer-decoded file, 100 listed entries, 1,000 MIME nodes, 30 nesting levels,
128 KiB metadata JSON, and 6 MiB retrieval JSON (including base64 overhead).
JSON limits count each ASCII-escaped serialized result object. MCP may
include both text and structured representations of that object, so transport
envelopes can be approximately twice that size plus protocol overhead. Oversized individual
files remain listed with their size and `retrievable: false`; retrieval fails
with a clear error. Exceeding message, tree, entry or JSON limits fails the
request rather than returning a silently truncated result. Validation and
limit errors use `{error: "...", status: 400}`; authentication and unexpected
failures follow existing tool conventions (401 and generic 500).

As in existing message reads, MIME parsing uses a full message held only in
memory, rather than a custom IMAP BODYSTRUCTURE parser. Both calls first
check `RFC822.SIZE`, then request at most 16 MiB + 1 with partial `BODY.PEEK`,
and verify the returned literal size. Thus even retrieval of a small file
from a message larger than 16 MiB is refused. Only the selected attachment
is returned to the MCP client. Limits bound normal server responses; an
IMAP server violating the requested partial-literal bound may still allocate
memory inside the underlying IMAP library before our size check.

## Quick start with Docker Compose

Create an Apple app-specific password, then:

```bash
cp .env.example .env
# Set MCP_AUTH_TOKEN, ICLOUD_EMAIL and ICLOUD_APP_SPECIFIC_PASSWORD.
docker compose up -d
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. HTTP startup fails closed
when neither `MCP_AUTH_TOKEN` nor `MCP_CLIENTS_JSON` is configured.

## Unraid

1. Copy `unraid/icloud-mcp.xml` to
   `/boot/config/plugins/dockerMan/templates-user/my-icloud-mcp.xml`, or add
   its raw GitHub URL through Unraid's template workflow.
2. Set a long random **MCP Bearer Token**, the iCloud email, and an Apple
   app-specific password.
3. Keep delete switches disabled until a dedicated agent actually needs them.
4. Install the container. No companion database, proxy, or sidecar is needed.

The template applies `--read-only`, `--cap-drop=ALL`,
`no-new-privileges`, a bounded `/tmp` tmpfs, and runs as UID/GID 10001.
The published GHCR image supports AMD64 and ARM64.
Its only writable persistent path is `/mnt/user/appdata/icloud-mcp` mapped to
`/data`; include that directory in the normal Unraid appdata backup.

## Per-agent access

The legacy `MCP_AUTH_TOKEN` grants all scopes. For multiple agents, use
`MCP_CLIENTS_JSON` (or preferably `MCP_CLIENTS_JSON_FILE`):

```json
[
  {
    "id": "inbox-reader",
    "token": "long-random-token-1",
    "scopes": ["mail:read", "calendar:read"]
  },
  {
    "id": "assistant",
    "token": "long-random-token-2",
    "scopes": [
      "mail:read", "mail:write", "mail:send",
      "calendar:read", "calendar:write",
      "contacts:read"
    ]
  }
]
```

Available scopes are:

| Area | Read | Change | Delete / send |
|---|---|---|---|
| Calendar | `calendar:read` | `calendar:write` | `calendar:delete` |
| Contacts | `contacts:read` | `contacts:write` | `contacts:delete` |
| Mail | `mail:read` | `mail:write` | `mail:delete`, `mail:send` |

Scopes and server capability switches are both enforced. For example,
`mail:delete` is insufficient while `ENABLE_MAIL_DELETE=false`.

## Configuration

Every setting is supplied by environment variable. Secret values also support
a same-named `_FILE` variant, such as
`ICLOUD_APP_SPECIFIC_PASSWORD_FILE=/run/secrets/apple-password`.

| Variable | Default | Purpose |
|---|---:|---|
| `MCP_AUTH_TOKEN` | empty | Backward-compatible all-scope bearer token |
| `MCP_CLIENTS_JSON(_FILE)` | empty | Per-client tokens and scopes |
| `MCP_REQUIRE_AUTH` | `true` | Require auth for HTTP |
| `MCP_ALLOW_UNAUTHENTICATED_HTTP` | `false` | Explicit emergency opt-out |
| `MCP_TRUST_STDIO` | `true` | Trust locally spawned stdio clients |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | HTTP bind address and port |
| `ICLOUD_EMAIL(_FILE)` | empty | iCloud account |
| `ICLOUD_APP_SPECIFIC_PASSWORD(_FILE)` | empty | Apple app password |
| `ICLOUD_CONTACTS_EMAIL(_FILE)` | empty | Optional separate Apple account for contacts |
| `ICLOUD_CONTACTS_APP_SPECIFIC_PASSWORD(_FILE)` | empty | App password for the separate contacts account; set both contacts settings together |
| `ICLOUD_MAIL_EMAIL(_FILE)` | iCloud account | Actual iCloud Mail address when the Apple ID uses another address |
| `ICLOUD_MAIL_APP_SPECIFIC_PASSWORD(_FILE)` | Apple app password | Optional separate app password for iCloud Mail |
| `ICLOUD_ALLOW_HEADER_CREDENTIALS` | `false` | Permit per-request Apple credentials |
| `EMAIL_SEND_FROM_ALLOWLIST` | empty | Exact comma-separated From aliases for the configured mail account; empty permits only the login address |
| `EMAIL_SEND_ALLOWLIST` | empty | Exact comma-separated recipients; applies to SMTP and calendar invitations |
| `ENABLE_CALENDAR_WRITE` | `true` | Create/update calendar events |
| `ENABLE_CALENDAR_DELETE` | `false` | Delete calendar events |
| `ENABLE_CALENDAR_INVITATIONS` | `false` | Send iTIP invitations/cancellations; also requires `mail:send` |
| `ENABLE_CONTACTS_WRITE` | `true` | Create/update contacts |
| `ENABLE_CONTACTS_DELETE` | `false` | Delete contacts |
| `ENABLE_MAIL_WRITE` | `true` | Move mail or change read state |
| `ENABLE_MAIL_DELETE` | `false` | Move mail to trash/delete |
| `ENABLE_PERMANENT_MAIL_DELETE` | `false` | Permanently expunge mail |
| `ENABLE_MAIL_SEND` | `true` | Send SMTP mail and iTIP invitations |
| `RESOURCE_ID_SIGNING_KEY(_FILE)` | empty | Return HMAC-signed opaque DAV IDs |
| `ALLOW_LEGACY_URL_IDS` | `true` | Accept old raw URL IDs during migration |
| `AUDIT_LOG_ENABLED` | `true` | JSON authorization audit events to stderr |
| `AUDIT_HMAC_KEY(_FILE)` | built-in fallback | Stable, private hashes for audit targets |
| `IDEMPOTENCY_STORE_PATH` | `/tmp/icloud-mcp-idempotency.sqlite3` | SQLite replay store |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | Idempotency retention |
| `HTTP_TIMEOUT` | `30` | Outbound network timeout |
| `DISABLE_HTTP3` | `true` | Use HTTP/1.1 or HTTP/2 for NAS/kernel compatibility |
| `DEFAULT_TZ` | system timezone | Zone for naive calendar timestamps |
| `CALDAV_SERVER` | Apple CalDAV | CalDAV base URL |
| `CARDDAV_SERVER` | Apple CardDAV | CardDAV base URL |
| `IMAP_SERVER` / `IMAP_PORT` | Apple / `993` | IMAP endpoint |
| `SMTP_SERVER` / `SMTP_PORT` | Apple / `587` | SMTP endpoint |
| `CALDAV_ALLOWED_HOSTS` | Apple CalDAV hosts | Exact/wildcard credential egress policy |
| `CARDDAV_ALLOWED_HOSTS` | Apple CardDAV hosts | Exact/wildcard credential egress policy |

For persistent idempotency across container recreation, mount a writable
directory and point `IDEMPOTENCY_STORE_PATH` into it. The default stays in the
ephemeral tmpfs so the rest of the container can remain read-only.

### Separate contacts account

To read contacts and birthdays from another Apple account, set both
`ICLOUD_CONTACTS_EMAIL` and `ICLOUD_CONTACTS_APP_SPECIFIC_PASSWORD` (or their
`_FILE` variants). All contact operations, including create, update and delete,
then use that account. Calendar and Mail retain their existing credentials.
The existing contact scopes and write/delete switches still apply.

When both contacts settings are empty, contacts use `ICLOUD_EMAIL` and
`ICLOUD_APP_SPECIFIC_PASSWORD` as before. If only one contacts setting is
provided, contact operations fail rather than combining credentials from
different accounts. Explicit per-request Apple headers still take precedence
when `ICLOUD_ALLOW_HEADER_CREDENTIALS=true`; they remain disabled by default.
After switching accounts, refresh contact IDs from `contacts_list`.

## Signed ID migration

Set `RESOURCE_ID_SIGNING_KEY_FILE` to make newly returned Calendar and Contact
IDs opaque and tamper-evident. Existing raw URL IDs remain accepted while
`ALLOW_LEGACY_URL_IDS=true`, so old agents continue to work. Once all clients
have refreshed their IDs, set it to `false`.

## Security notes

- Never expose plain HTTP over the public Internet. Put the endpoint behind a
  TLS reverse proxy or a private overlay network.
- Apple credentials are server-side by default. Header credentials are opt-in
  because a shared HTTP service should not accept arbitrary Apple passwords.
- DAV resource URLs are restricted to the configured HTTPS host patterns and
  port 443 before credentials are sent.
- Audit records contain client IDs, authorization decisions, scopes, and keyed target hashes;
  they do not contain bearer tokens, Apple passwords, message bodies, or event
  contents.
- Empty `EMAIL_SEND_ALLOWLIST` preserves upstream behavior and allows every
  recipient. Configure it when agents must only contact known addresses.

## Local development

```bash
uv sync --frozen --extra dev --python 3.12
uv run pytest -q
uv run ruff check src tests
docker build -t icloud-mcp:dev .
```

The dependency graph is committed in `uv.lock`. CI tests Python 3.12 and
publishes signed-provenance multi-platform images from the fork's `main`
branch.

## Upstream compatibility

This branch contains PR #22 as its first four commits, followed by isolated
hardening and packaging commits. Existing tool names and original arguments are
unchanged; additive parameters are optional. `MCP_AUTH_TOKEN`, raw URL IDs,
and stdio operation remain available for migration and upstream merges.

MIT licensed; see `LICENSE`.

### Selecting a mail sender

`email_send` accepts optional `from_address`, for example
`"from_address": "john@bechara.org"`. Omitting it (or passing `null`) keeps the
mail login address as the visible sender. To enable an alias after deployment,
set `EMAIL_SEND_FROM_ALLOWLIST=john@bechara.org` (comma-separated for multiple
addresses). This is separate from the recipient `EMAIL_SEND_ALLOWLIST`.

Only exact addresses are approved, compared case-insensitively: no domains,
wildcards, display names, address lists, whitespace, control characters or
non-ASCII mailboxes. A caller may always select its own mail login address.
Aliases are restricted to `ICLOUD_MAIL_EMAIL` (falling back to `ICLOUD_EMAIL`);
per-request credentials for a different account cannot use this alias list.
All clients with `mail:send` on the configured account share the approved aliases.

The alias must already be enabled for that same iCloud Mail account. This
includes custom-domain addresses set up in iCloud+; merely receiving catch-all
mail does not establish an approved sending identity. See Apple's
[alias documentation](https://support.apple.com/en-ca/guide/icloud/mm6b1a490a/icloud)
and [custom-domain setup](https://support.apple.com/en-asia/guide/iphone/iph4de56a8a2/27/ios/27).
The local allowlist is an administrator assertion, not provider-side ownership
verification. Do not approve addresses belonging to other accounts.

Selection changes the MIME `From` header, including the stored Sent copy.
SMTP authentication, SMTP envelope `MAIL FROM`, and IMAP authentication continue
to use the mail login address. Provider rejection or rewriting remains possible;
SMTP acceptance and delivery of aliases have not been tested with real mail.
No SMTPUTF8 or display-name support is added.

The selected `from_address` participates in request-ID matching; changing it
requires a new `request_id`. Omitted/null sender calls retain the existing
idempotency fingerprint, so pre-upgrade cached responses still replay.
