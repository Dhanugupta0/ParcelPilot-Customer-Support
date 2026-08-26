# AI Tool Usage

**Claude Code (Anthropic)** — used throughout, as a pair rather than an
autocomplete.

Where it did the most work:

- **Reading the data pack first.** The six PDFs and the workbook were extracted
  and read before any code was written; the rule table in `engine.py` and the
  test expectations come from that reading, not from assumption.
- **The deterministic engine and its tests.** Each rule was written alongside a
  test naming the clause it enforces, so the document pack and the code can be
  checked against each other.
- **Finding cases I had missed.** Two examples worth naming: the dataset
  snapshot is a **Sunday**, which combined with LumenWorks' "no weekend cover"
  clause changes TKT-502 from breached to within target; and a question phrased
  *"am I owed anything?"* originally bypassed the calculation tool, letting the
  model read a figure out of a policy document instead of computing it.

What I checked rather than accepted:

- Every figure the engine produces is asserted against the source PDF in
  `tests/`. Where a generated answer and the document disagreed, the document
  won.
- The first architecture I built removed tool-calling entirely. Reading the
  assessment brief — which was in the data pack as a `.docx` — showed that
  violated three mandatory requirements, and the design was reworked so the
  agent chooses tools while the tools still do the arithmetic.

The judgement calls — deterministic engine over model reasoning, vectors for
documents but SQL for records, contract terms transcribed rather than parsed,
what to show a customer versus an agent — are mine, and the reasoning for each
is written in `ARCHITECTURE.md` and in the module docstrings.
