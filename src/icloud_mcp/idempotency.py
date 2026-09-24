"""Small persistent idempotency store for externally visible create/send calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .config import config


def _connect() -> sqlite3.Connection:
    path = Path(config.IDEMPOTENCY_STORE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        "key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created REAL NOT NULL, "
        "state TEXT NOT NULL, response TEXT)"
    )
    return db


async def run_once(principal: str, action: str, request_id: str | None, payload, operation):
    if not request_id:
        return await operation()
    if len(request_id) > 200:
        raise ValueError("request_id is too long")
    key = f"{principal}:{action}:{request_id}"
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    now = time.time()
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM requests WHERE created < ?", (now - config.IDEMPOTENCY_TTL_SECONDS,))
        row = db.execute(
            "SELECT fingerprint, state, response FROM requests WHERE key = ?", (key,)
        ).fetchone()
        if row:
            db.execute("COMMIT")
            if row[0] != fingerprint:
                raise ValueError("request_id was already used with different arguments")
            if row[1] == "done":
                return json.loads(row[2])
            raise ValueError("A request with this request_id is already in progress")
        db.execute(
            "INSERT INTO requests(key, fingerprint, created, state) VALUES (?, ?, ?, 'pending')",
            (key, fingerprint, now),
        )
        db.execute("COMMIT")
    finally:
        db.close()

    try:
        result = await operation()
    except BaseException:
        db = _connect()
        try:
            db.execute("DELETE FROM requests WHERE key = ? AND state = 'pending'", (key,))
        finally:
            db.close()
        raise

    db = _connect()
    try:
        db.execute(
            "UPDATE requests SET state = 'done', response = ? WHERE key = ?",
            (json.dumps(result, default=str), key),
        )
    finally:
        db.close()
    return result
