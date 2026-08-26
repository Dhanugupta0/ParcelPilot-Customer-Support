"""The last check before an answer is shown.

Every figure the model was allowed to state came from a tool result, and the
system prompt tells it to quote them exactly. This confirms that it did.

It should never fire. It exists because "should never" is not a guarantee, and
a wrong number in a customer reply is the single failure this whole design is
arranged to prevent. It reports rather than blocks: the tool trace beside the
answer is already correct, so the useful thing is to mark which sentence is not.
"""
from __future__ import annotations

import json
import re

_FIGURE = re.compile(
    r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)"
    r"|\b(\d+(?:\.\d+)?)[\s-]*(?:minute|min|hour|hr|h|day|week|row)s?\b", re.I)

# Every number a tool result contains, whatever it is attached to. The prose
# side stays narrow -- only figures carrying a currency or a time unit are
# claims worth checking -- but the ALLOWED side must be wide, because a figure
# is legitimate wherever the tool happened to put it. Reading the tool blob with
# `_FIGURE` was matching only numbers a unit word followed, so "30-minute rule"
# in a rule chain and `"overdue_minutes": 120` in a facts dict both failed to
# register, and every correct answer quoting them was flagged as fabricated.
_ANY_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Small integers are structural ("2 tickets", "step 3"), not factual claims.
_STRUCTURAL = {str(n) for n in range(0, 25)}

# The model writes figures with typographic spaces and dashes ("30 minutes"
# with U+202F, "INR 300" with U+2011). Normalised before matching so that a
# number is compared on its value and not on its punctuation.
_SPACE = dict.fromkeys(map(ord, "\u00a0\u2007\u202f\u2009\u200a"), " ")
_DASH = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014"), "-")


def _norm(s: str) -> str:
    return (s or "").translate(_SPACE).translate(_DASH)


def _values(raw: str) -> set[str]:
    """A number, in the forms it may legitimately be written back as.

    `120` and `120.0` are the same figure; the engine emits one and the model
    may echo the other.
    """
    out = {raw}
    try:
        f = float(raw)
    except ValueError:
        return out
    out.add(f"{f:g}")
    if f.is_integer():
        out.add(str(int(f)))
        out.add(f"{f:.1f}")
    return out

# Record ids get the same treatment as figures: a fabricated "TCK-5678" sends an
# agent to look up something that never existed.
_ID = re.compile(r"\b((?:TKT|TCK|SYN|ORD|ACCT|ACC|KI|PROP|ESC)[-‑–_]?\d{2,})\b",
                 re.I)


def _norm_id(s: str) -> str:
    return re.sub(r"[-‑–_\s]", "", s).upper()


def check(answer: str, tool_results: list[dict], question: str = "") -> list[str]:
    """Figures and record ids in the prose that no tool result contains."""
    blob = _norm(json.dumps([t.get("result") for t in tool_results], default=str)
                 + " " + (question or ""))
    answer = _norm(answer or "")

    allowed = set(_STRUCTURAL)
    for m in _ANY_NUMBER.finditer(blob):
        allowed |= _values(m.group(0))
    allowed_ids = {_norm_id(m.group(1)) for m in _ID.finditer(blob)}

    out: list[str] = []
    for m in _FIGURE.finditer(answer):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        surface = m.group(0).strip()
        if raw and not (_values(raw) & allowed) and surface not in out:
            out.append(surface)
    for m in _ID.finditer(answer):
        if _norm_id(m.group(1)) not in allowed_ids and m.group(1) not in out:
            out.append(m.group(1))
    return out
