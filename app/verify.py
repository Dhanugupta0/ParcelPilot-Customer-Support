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
    r"|\b(\d+(?:\.\d+)?)\s*(?:minute|min|hour|hr|day|week|row)s?\b", re.I)

# Small integers are structural ("2 tickets", "step 3"), not factual claims.
_STRUCTURAL = {str(n) for n in range(0, 25)}

# Record ids get the same treatment as figures: a fabricated "TCK-5678" sends an
# agent to look up something that never existed.
_ID = re.compile(r"\b((?:TKT|TCK|SYN|ORD|ACCT|ACC|KI|PROP|ESC)[-‑–_]?\d{2,})\b",
                 re.I)


def _norm_id(s: str) -> str:
    return re.sub(r"[-‑–_\s]", "", s).upper()


def check(answer: str, tool_results: list[dict], question: str = "") -> list[str]:
    """Figures and record ids in the prose that no tool result contains."""
    blob = json.dumps([t.get("result") for t in tool_results], default=str) \
        + " " + (question or "")

    allowed = {(m.group(1) or m.group(2) or "").replace(",", "")
               for m in _FIGURE.finditer(blob)} | _STRUCTURAL
    allowed_ids = {_norm_id(m.group(1)) for m in _ID.finditer(blob)}

    out: list[str] = []
    for m in _FIGURE.finditer(answer or ""):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        surface = m.group(0).strip()
        if raw and raw not in allowed and surface not in out:
            out.append(surface)
    for m in _ID.finditer(answer or ""):
        if _norm_id(m.group(1)) not in allowed_ids and m.group(1) not in out:
            out.append(m.group(1))
    return out
