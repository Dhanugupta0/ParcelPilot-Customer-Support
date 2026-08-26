"""Durable conversation store.

Until now every conversation lived in a dict that died with the process. That
was defensible while the system was a demo of a single turn, but it makes the
two things this product actually needs impossible: a customer cannot come back
to a thread, and a support agent cannot audit what the assistant told someone
last Tuesday.

SQLite rather than a server: the policy corpus, the workbook and the audit log
are already local files, so a database process would be the only thing in the
stack that has to be running for the app to boot. One file, no daemon, and it
survives a restart -- which is the whole requirement.

Everything a reviewer needs to VERIFY an answer is recorded, not just the prose:
the tool calls with their arguments and raw results, the citations, the trust
assessment, and whether the answer was withheld.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app import config

DB_PATH = config.VAR_DIR / "parcelpilot.db"

# One connection, guarded. SQLite handles concurrent readers fine, but the app
# is a single uvicorn worker and the writes are small, so a lock is simpler than
# a pool and removes any chance of interleaved partial writes.
_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  role          TEXT NOT NULL,
  account_id    TEXT,
  account_name  TEXT,
  title         TEXT,
  context_kind  TEXT,          -- 'ticket' | 'order' | NULL for free-form
  context_ref   TEXT,          -- TKT-502 / ORD-1001
  started_at    TEXT NOT NULL,
  last_at       TEXT NOT NULL,
  turns         INTEGER NOT NULL DEFAULT 0,
  escalated     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_conv_user    ON conversations(user_id, last_at DESC);
CREATE INDEX IF NOT EXISTS ix_conv_account ON conversations(account_id, last_at DESC);
CREATE INDEX IF NOT EXISTS ix_conv_role    ON conversations(role, last_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq             INTEGER NOT NULL,
  role            TEXT NOT NULL,       -- 'user' | 'assistant'
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  -- Trust assessment: assistant turns only. Recorded so a reviewer sees the
  -- confidence the customer was actually shown, not a recomputation.
  confidence      REAL,
  band            TEXT,
  reasons         TEXT,
  citations       TEXT,
  conflicts       TEXT,
  withheld        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS tool_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  turn            INTEGER NOT NULL,
  tool            TEXT NOT NULL,
  category        TEXT,
  args            TEXT,
  result          TEXT,
  summary         TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tool_conv ON tool_calls(conversation_id, turn);

CREATE TABLE IF NOT EXISTS escalations (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT,
  account_id      TEXT,
  account_name    TEXT,
  ticket_id       TEXT,
  severity        TEXT,
  reason          TEXT,
  details         TEXT,
  raised_by       TEXT,
  raised_by_role  TEXT,
  status          TEXT NOT NULL,       -- proposed | committed | declined
  reference       TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_esc_status ON escalations(status, created_at DESC);

-- Vector index over conversations. Kept in the same file so a conversation and
-- its embedding cannot drift apart or be backed up separately.
CREATE TABLE IF NOT EXISTS conversation_vectors (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  text            TEXT NOT NULL,
  dim             INTEGER NOT NULL,
  vector          BLOB NOT NULL,
  updated_at      TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    global _conn
    with _LOCK:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            # WAL keeps a reader (the employee console polling for escalations)
            # from blocking a writer (someone mid-conversation).
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def _rows(sql: str, args: Iterable = ()) -> list[dict]:
    with _LOCK:
        return [dict(r) for r in conn().execute(sql, tuple(args)).fetchall()]


def _one(sql: str, args: Iterable = ()) -> dict | None:
    r = _rows(sql, args)
    return r[0] if r else None


def _write(sql: str, args: Iterable = ()) -> None:
    with _LOCK:
        c = conn()
        c.execute(sql, tuple(args))
        c.commit()


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------

def create_conversation(principal, *, account_name: str | None = None,
                        context_kind: str | None = None,
                        context_ref: str | None = None,
                        title: str | None = None) -> str:
    cid = uuid.uuid4().hex[:12]
    ts = now()
    _write(
        """INSERT INTO conversations
           (id, user_id, display_name, role, account_id, account_name, title,
            context_kind, context_ref, started_at, last_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, principal.user_id, principal.display_name, principal.role.value,
         principal.account_id, account_name, title, context_kind, context_ref, ts, ts))
    return cid


def get_conversation(cid: str) -> dict | None:
    return _one("SELECT * FROM conversations WHERE id = ?", (cid,))


def touch_conversation(cid: str, *, title: str | None = None,
                       bump_turn: bool = False, escalated: bool | None = None) -> None:
    sets, args = ["last_at = ?"], [now()]
    if title:
        # Only ever set the title once, from the first question asked.
        sets.append("title = COALESCE(NULLIF(title, ''), ?)")
        args.append(title)
    if bump_turn:
        sets.append("turns = turns + 1")
    if escalated is not None:
        sets.append("escalated = ?")
        args.append(1 if escalated else 0)
    args.append(cid)
    _write(f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", args)


def list_conversations(*, user_id: str | None = None, account_id: str | None = None,
                       role: str | None = None, escalated_only: bool = False,
                       include_empty: bool = False, limit: int = 60) -> list[dict]:
    where, args = [], []
    if not include_empty:
        # A session opened and abandoned before anyone spoke has nothing in it
        # to read or review, and a review queue padded with blanks is a review
        # queue people stop trusting.
        where.append("turns > 0")
    if user_id:
        where.append("user_id = ?"); args.append(user_id)
    if account_id:
        where.append("account_id = ?"); args.append(account_id)
    if role:
        where.append("role = ?"); args.append(role)
    if escalated_only:
        where.append("escalated = 1")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    args.append(limit)
    rows = _rows(f"""SELECT * FROM conversations {clause}
                     ORDER BY last_at DESC LIMIT ?""", args)
    for r in rows:
        # A thread opened but never spoken in has no title yet. Label it here
        # so every caller does not have to handle the null itself.
        if not r.get("title"):
            r["title"] = (f"About {r['context_ref']}" if r.get("context_ref")
                          else "New conversation")
    return rows


# --------------------------------------------------------------------------
# Messages and tool calls
# --------------------------------------------------------------------------

def next_seq(cid: str) -> int:
    r = _one("SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE conversation_id = ?",
             (cid,))
    return int(r["s"]) + 1 if r else 1


def add_message(cid: str, seq: int, role: str, content: str,
                trust: dict | None = None) -> None:
    t = trust or {}
    _write("""INSERT INTO messages
              (conversation_id, seq, role, content, created_at,
               confidence, band, reasons, citations, conflicts, withheld)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
           (cid, seq, role, content, now(),
            t.get("confidence"), t.get("band"),
            json.dumps(t.get("reasons") or []),
            json.dumps(t.get("citations") or []),
            json.dumps(t.get("conflicts") or []),
            1 if t.get("withheld") else 0))


def add_tool_call(cid: str, turn: int, tool: str, category: str | None,
                  args: Any, result: Any, summary: str | None) -> None:
    _write("""INSERT INTO tool_calls
              (conversation_id, turn, tool, category, args, result, summary, created_at)
              VALUES (?,?,?,?,?,?,?,?)""",
           (cid, turn, tool, category,
            json.dumps(args, default=str)[:8000],
            json.dumps(result, default=str)[:20000],
            summary, now()))


def transcript(cid: str) -> dict:
    """Everything needed to audit one conversation, in one call."""
    convo = get_conversation(cid)
    if convo is None:
        return {}
    msgs = _rows("SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (cid,))
    for m in msgs:
        for k in ("reasons", "citations", "conflicts"):
            try:
                m[k] = json.loads(m[k] or "[]")
            except (TypeError, ValueError):
                m[k] = []
        m["withheld"] = bool(m["withheld"])
    tools = _rows("SELECT * FROM tool_calls WHERE conversation_id = ? ORDER BY id", (cid,))
    for t in tools:
        for k in ("args", "result"):
            try:
                t[k] = json.loads(t[k] or "null")
            except (TypeError, ValueError):
                pass
    return {"conversation": convo, "messages": msgs, "tool_calls": tools,
            "escalations": escalations_for(cid)}


# --------------------------------------------------------------------------
# Escalations
# --------------------------------------------------------------------------

def record_escalation(*, proposal_id: str, conversation_id: str | None,
                      account_id: str | None, account_name: str | None,
                      ticket_id: str | None, severity: str | None,
                      reason: str | None, details: str | None,
                      raised_by: str, raised_by_role: str,
                      status: str = "proposed") -> None:
    ts = now()
    _write("""INSERT OR REPLACE INTO escalations
              (id, conversation_id, account_id, account_name, ticket_id, severity,
               reason, details, raised_by, raised_by_role, status, reference,
               created_at, updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,
                      (SELECT reference FROM escalations WHERE id = ?),
                      COALESCE((SELECT created_at FROM escalations WHERE id = ?), ?), ?)""",
           (proposal_id, conversation_id, account_id, account_name, ticket_id,
            severity, reason, details, raised_by, raised_by_role, status,
            proposal_id, proposal_id, ts, ts))


def set_escalation_status(proposal_id: str, status: str,
                          reference: str | None = None) -> None:
    _write("""UPDATE escalations SET status = ?, reference = COALESCE(?, reference),
              updated_at = ? WHERE id = ?""", (status, reference, now(), proposal_id))


def escalations_for(cid: str) -> list[dict]:
    return _rows("SELECT * FROM escalations WHERE conversation_id = ? ORDER BY created_at",
                 (cid,))


def list_escalations(limit: int = 100) -> list[dict]:
    return _rows("""SELECT * FROM escalations
                    ORDER BY CASE status WHEN 'proposed' THEN 0
                                         WHEN 'committed' THEN 1 ELSE 2 END,
                             created_at DESC
                    LIMIT ?""", (limit,))


# --------------------------------------------------------------------------
# Vectors (see store/vectors.py for the embedding side)
# --------------------------------------------------------------------------

def upsert_vector(cid: str, text: str, dim: int, blob: bytes) -> None:
    _write("""INSERT INTO conversation_vectors (conversation_id, text, dim, vector, updated_at)
              VALUES (?,?,?,?,?)
              ON CONFLICT(conversation_id) DO UPDATE SET
                text = excluded.text, dim = excluded.dim,
                vector = excluded.vector, updated_at = excluded.updated_at""",
           (cid, text, dim, blob, now()))


def all_vectors() -> list[dict]:
    return _rows("SELECT conversation_id, dim, vector, text FROM conversation_vectors")


def stats() -> dict:
    def n(sql):
        r = _one(sql)
        return int(list(r.values())[0]) if r else 0
    return {
        "conversations": n("SELECT COUNT(*) FROM conversations"),
        "messages": n("SELECT COUNT(*) FROM messages"),
        "tool_calls": n("SELECT COUNT(*) FROM tool_calls"),
        "escalations": n("SELECT COUNT(*) FROM escalations"),
        "indexed": n("SELECT COUNT(*) FROM conversation_vectors"),
    }
