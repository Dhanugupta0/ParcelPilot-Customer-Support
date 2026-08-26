"""Append-only audit log.

Every tool invocation, proposal, commit and access denial is recorded. For a
system that can issue credits and change ticket state, "what did it do and on
whose authority" has to be answerable after the fact without re-running
anything. Newline-delimited JSON keeps it greppable and append-only.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app import config

_LOCK = threading.Lock()
_PATH = config.VAR_DIR / "audit.jsonl"


def record(event: str, principal, **fields: Any) -> dict:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "actor": getattr(principal, "user_id", "system"),
        "role": getattr(getattr(principal, "role", None), "value", "system"),
        "actor_account": getattr(principal, "account_id", None),
        **fields,
    }
    with _LOCK:
        with _PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def tail(limit: int = 50) -> list[dict]:
    if not _PATH.exists():
        return []
    lines = _PATH.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines[-limit:]]
