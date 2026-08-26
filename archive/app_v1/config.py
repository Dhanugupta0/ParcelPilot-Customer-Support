"""Central configuration.

Every tunable that the data pack does not define lives here as an EXPLICIT,
documented assumption rather than being buried in code. The assessment brief
invites assumptions; it does not excuse hiding them.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "Dataset"
VAR_DIR = ROOT / "var"          # runtime state: audit log, mocked action store
VAR_DIR.mkdir(exist_ok=True)

WORKBOOK = DATASET_DIR / "ParcelPilot_Assessment_Data.xlsx"

# --- Time -------------------------------------------------------------------
# The README sheet fixes the reference time for ALL time-based reasoning.
# Read from the workbook at startup (see core.clock) but pinned here as a
# fallback so the system is never accidentally driven by real wall-clock time.
TIMEZONE = ZoneInfo("Asia/Kolkata")
SNAPSHOT_FALLBACK = "2026-08-16 11:00"

# ASSUMPTION: the pack never defines "business hours". We adopt a standard
# Indian B2B working week. Exposed here so a reviewer can change it in one place
# and re-run the eval suite to see exactly which answers move.
BUSINESS_DAYS = {0, 1, 2, 3, 4}          # Mon-Fri (Python weekday numbering)
BUSINESS_DAY_START = (9, 0)              # 09:00 IST
BUSINESS_DAY_END = (18, 0)               # 18:00 IST
# ASSUMPTION: no public-holiday calendar was supplied, so none is applied.
HOLIDAYS: set[str] = set()

# --- Money ------------------------------------------------------------------
CURRENCY = "INR"

# --- LLM --------------------------------------------------------------------
# The provider is Groq, reached through its OpenAI-compatible endpoint, so the
# official `openai` SDK is still the client -- only the base URL changes. That
# keeps the tool-calling and streaming code below provider-agnostic.
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

_raw_key = os.getenv("GROQ_API_KEY", "").strip()
# Treat the placeholder from .env.example as absent, so a forgotten key fails
# at startup with a clear message instead of as an opaque 401 mid-conversation.
GROQ_API_KEY = "" if _raw_key in {"", "gsk_...", "your-key-here"} else _raw_key

# The reasoning model that drives the agent loop.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# A smaller model is enough for the mechanical prose/grading passes.
GROQ_UTILITY_MODEL = os.getenv("GROQ_UTILITY_MODEL", "qwen/qwen3.6-27b")

# Both models on Groq are reasoning models: left alone they emit a chain of
# thought that either streams to the user as noise or -- on qwen -- arrives
# wrapped in <think> tags inside `content` and lands in a customer-facing email.
# `reasoning_format=hidden` keeps it server-side; see core/llm.py.
GROQ_REASONING_FORMAT = os.getenv("GROQ_REASONING_FORMAT", "hidden")

# ASSUMPTION: reasoning effort is per-model vocabulary, not a shared scale.
# gpt-oss accepts low|medium|high; qwen3.6 accepts none|default and burns its
# whole token budget on thinking unless told "none". Wrong value = HTTP 400, so
# each is configured separately and an empty string omits the parameter.
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "medium")
GROQ_UTILITY_REASONING_EFFORT = os.getenv("GROQ_UTILITY_REASONING_EFFORT", "none")

MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))

# --- Behaviour --------------------------------------------------------------
# Below this confidence the agent refuses to answer and offers escalation
# instead. See agent/trust.py -- confidence is DERIVED from signals, never
# self-reported by the model.
MIN_ANSWER_CONFIDENCE = 0.55
