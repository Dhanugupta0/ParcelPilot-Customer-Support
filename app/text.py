"""Turning internal phrasing into customer phrasing.

One home for it, because the rule is the same everywhere: a customer needs the
substance of a warning without our vocabulary. They do not need to know an
issue is tracked as KI-211 or which document it lives in — they need to know the
status might be stale.
"""
from __future__ import annotations

import re

_CODE = re.compile(r"^(KI-\d+|[A-Z]{2,4}-\d+)\s*[:—-]\s*")
_CITE = re.compile(r"\s*\((?:Product Operations Guide|Support Policy|"
                   r"Cancellation & Service Credit SOP)[^)]*\)\s*$")


def plainly(caveat: str) -> str:
    out = _CITE.sub("", _CODE.sub("", (caveat or "").strip()))
    return out[0].upper() + out[1:] if out else out
