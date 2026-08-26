"""The single place that knows which model provider we talk to.

Groq speaks the OpenAI wire protocol, so the `openai` SDK stays the client and
only the base URL moves. Centralising that here means the agent loop and the
outreach writer never name a provider: swapping vendors is a change to this
file and to config, not to the reasoning code.

It also owns the two Groq-specific details that would otherwise be duplicated:
the reasoning-control parameters, and the safety net for models that leak their
chain of thought into `content` anyway.
"""
from __future__ import annotations

import re

from openai import OpenAI

from app import config

_client: OpenAI | None = None


def client() -> OpenAI:
    """The shared Groq client, built once.

    Raises with an actionable message rather than letting a missing key surface
    as a 401 in the middle of someone's conversation.
    """
    global _client
    if _client is None:
        if not config.GROQ_API_KEY:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and add your "
                "Groq key (console.groq.com/keys).")
        _client = OpenAI(api_key=config.GROQ_API_KEY,
                         base_url=config.GROQ_BASE_URL)
    return _client


def reasoning_params(effort: str | None = None) -> dict:
    """Groq's reasoning controls, as an `extra_body` payload.

    These are not part of the OpenAI schema, so the SDK silently DROPS them when
    passed as ordinary keyword arguments -- they only take effect inside
    `extra_body`. Empty config values omit the parameter entirely, which matters
    because the accepted vocabulary differs per model and a wrong value is a 400,
    not a warning.
    """
    body: dict = {}
    if config.GROQ_REASONING_FORMAT:
        body["reasoning_format"] = config.GROQ_REASONING_FORMAT
    effort = config.GROQ_REASONING_EFFORT if effort is None else effort
    if effort:
        body["reasoning_effort"] = effort
    return body


def utility_reasoning_params() -> dict:
    """As above, for the smaller model used on the mechanical passes."""
    return reasoning_params(config.GROQ_UTILITY_REASONING_EFFORT)


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>",
                    re.DOTALL | re.IGNORECASE)
_UNCLOSED = re.compile(r"^\s*<(think|thinking|reasoning)>.*$",
                       re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove any chain of thought the model inlined into its answer.

    `reasoning_format=hidden` is meant to make this unnecessary, but it is a
    provider-side convenience, not a guarantee: a model that hits its token
    budget mid-thought can still return an unterminated <think> block. The cost
    of being wrong here is a customer receiving an email containing the model's
    private deliberation, so the belt-and-braces check stays.
    """
    if not text or "<" not in text:
        return text
    out = _THINK.sub("", text)
    # An opening tag with no close means the thought never finished; there is no
    # answer after it to keep.
    if _UNCLOSED.match(out):
        return ""
    return out.strip()
