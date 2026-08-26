"""The single model call, and the only place a provider is named.

Groq via its OpenAI-compatible endpoint. There is no tool-calling here and no
loop: the pipeline has already decided everything by the time this runs, so the
request is one system prompt, one payload, one completion.
"""
from __future__ import annotations

import re
import threading

from openai import OpenAI

from app import config

_client: OpenAI | None = None
_lock = threading.Lock()


def client() -> OpenAI:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                if not config.GROQ_API_KEY:
                    raise RuntimeError(
                        "GROQ_API_KEY is not set. Copy .env.example to .env and "
                        "add your key from console.groq.com/keys.")
                _client = OpenAI(api_key=config.GROQ_API_KEY,
                                 base_url=config.GROQ_BASE_URL)
    return _client


def complete(system: str, user: str, *, max_tokens: int = 700) -> str:
    r = client().chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.2, max_tokens=max_tokens,
        # gpt-oss is a reasoning model; without this its chain of thought
        # arrives as its own field and can spill into the reply.
        extra_body={"reasoning_format": "hidden",
                    "reasoning_effort": config.GROQ_REASONING_EFFORT or "medium"},
    )
    return r.choices[0].message.content or ""


# --------------------------------------------------------------------------
# Retrying a rate limit
# --------------------------------------------------------------------------

_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?$")
MAX_BACKOFF_S = 45.0


def _parse_duration(raw: str) -> float | None:
    """Seconds from a plain number or Groq's "2h38m24s" / "577ms" form."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    m = _DURATION.match(raw)
    if not m or not any(m.groups()):
        return None
    h, mins, secs, ms = m.groups()
    return (float(h or 0) * 3600 + float(mins or 0) * 60
            + float(secs or 0) + float(ms or 0) / 1000)


def daily_quota_spent(e: Exception) -> bool:
    """A per-minute burst clears in seconds; a daily allowance does not.

    Both arrive as a 429. Backing off three times against a window that reopens
    tomorrow is pure delay, so they are told apart before anything waits.
    """
    body = f"{getattr(e, 'body', '')} {e}".lower()
    if any(k in body for k in ("tokens per day", "requests per day", "(tpd)", "(rpd)")):
        return True
    h = getattr(getattr(e, "response", None), "headers", None)
    return bool(h) and str(h.get("x-should-retry", "")).lower() == "false"


def retry_delay(e: Exception, attempt: int) -> float:
    """Prefer the reset the provider stated over an exponential guess."""
    h = getattr(getattr(e, "response", None), "headers", None)
    if h:
        for name in ("retry-after", "x-ratelimit-reset-tokens"):
            secs = _parse_duration(str(h.get(name) or ""))
            if secs and secs > 0:
                return min(secs + 0.25, MAX_BACKOFF_S)
    return min(1.5 * (2 ** attempt), MAX_BACKOFF_S)
