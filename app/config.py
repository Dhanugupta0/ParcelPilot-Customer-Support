"""Every tunable in one file, as an explicit assumption rather than a constant
buried in code. A reviewer should be able to read this and know what the system
believes about the world.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "Dataset"
VAR_DIR = ROOT / "var"
VAR_DIR.mkdir(exist_ok=True)
WORKBOOK = DATASET_DIR / "ParcelPilot_Assessment_Data.xlsx"

# --- Time -------------------------------------------------------------------
# The workbook's README sheet fixes the reference time for ALL time-based
# reasoning. Never wall-clock: an SLA answer that changes because the demo was
# re-run on a Tuesday is not a deterministic answer.
TIMEZONE = ZoneInfo("Asia/Kolkata")
SNAPSHOT_FALLBACK = "2026-08-16 11:00"

# ASSUMPTION: the document pack never defines "business hours". A standard
# Indian B2B working week is adopted. Change these two lines and every
# business-hours answer moves — the test suite will show exactly which.
BUSINESS_DAYS = {0, 1, 2, 3, 4}          # Mon–Fri
BUSINESS_DAY_START = (9, 0)
BUSINESS_DAY_END = (18, 0)
# ASSUMPTION: no public-holiday calendar was supplied, so none is applied.
HOLIDAYS: set[str] = set()

CURRENCY = "INR"

# --- Model ------------------------------------------------------------------
# Groq via its OpenAI-compatible endpoint. The model writes prose and nothing
# else: every figure it is given has already been computed in engine.py.
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
_raw = os.getenv("GROQ_API_KEY", "").strip()
GROQ_API_KEY = "" if _raw in {"", "gsk_...", "your-key-here"} else _raw
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# gpt-oss is a reasoning model; hidden keeps its chain of thought out of the
# reply, where it would otherwise arrive as prose.
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "medium")

# Jina Embeddings API for the policy vector store. Replaces the old local
# sentence-transformers model — no more PyTorch dependency.
JINA_API_KEY = os.getenv("JINA_EMBED_MODEL", "")
JINA_EMBED_MODEL_NAME = os.getenv("JINA_EMBED_MODEL_NAME", "jina-embeddings-v2-base-en")
