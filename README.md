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
- Contacts: `contacts_list`, `contacts_get`, `contacts_search`,
  `contacts_create`, `contacts_update`, `contacts_delete`
- Mail: `email_list_folders`, `email_list_messages`, `email_get_message`,
  `email_get_messages`, `email_search`, `email_send`, `email_move`,
  `email_delete`, `email_mark_read`, `email_mark_unread`

`calendar_create_event`, `contacts_create`, and `email_send` accept an
optional `request_id`. Repeating the same request ID and arguments returns the
stored result without repeating the external action.

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
| `ICLOUD_ALLOW_HEADER_CREDENTIALS` | `false` | Permit per-request Apple credentials |
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
