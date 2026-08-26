"""Run before deploying. Proves the box can serve a real answer.

Checks the things that actually break on a fresh host, in the order they break:
the document pack, the database, the embedding model, then the API key.
"""
from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

FAIL = 0


def check(label: str, fn):
    global FAIL
    try:
        print(f"  {label:.<46}", end="")
        detail = fn()
        print(f" ok  {detail or ''}")
    except Exception as e:                                        # noqa: BLE001
        FAIL += 1
        print(f" FAIL\n      {type(e).__name__}: {e}")


def main() -> int:
    print("\nParcelPilot preflight\n")

    from app import config
    check("document pack present", lambda: (
        f"{len(list(config.DATASET_DIR.glob('*.pdf')))} PDFs"
        if config.WORKBOOK.exists() else _raise("workbook missing")))

    from app import store
    check("workbook loads into SQLite", lambda: str(store.load_workbook()))
    check("snapshot time readable", lambda: f"{store.snapshot():%Y-%m-%d %H:%M %Z}")

    from app import engine
    check("engine: ORD-1001 waived by contract", lambda: (
        "INR 0" if engine.cancellation("ORD-1001").amount_inr == 0.0
        else _raise("expected INR 0")))
    check("engine: weekend clock (TKT-502)", lambda: (
        engine.sla("TKT-502").facts["due_at"]
        if not engine.sla("TKT-502").facts["breached"]
        else _raise("weekend cover not applied")))

    from app import knowledge
    check("policy index builds", lambda: f"{len(knowledge.index().chunks)} chunks")

    check("model key configured", lambda: (
        f"{config.GROQ_MODEL}" if config.GROQ_API_KEY
        else _raise("GROQ_API_KEY is not set")))

    print()
    if FAIL:
        print(f"{FAIL} check(s) failed — do not deploy.\n")
        return 1
    print("All checks passed. Safe to deploy.\n")
    return 0


def _raise(msg):
    raise RuntimeError(msg)


if __name__ == "__main__":
    raise SystemExit(main())
