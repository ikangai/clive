"""Audit-log-driven driver refinement (gh#41 Phase 3).

``refine_driver`` re-synthesizes a ``drivers/<name>.md`` from accumulated
eval-failure signals: it loads the current driver (canonical location
first, quarantine ``drivers/.unreviewed/`` as fallback), feeds it plus the
failure evidence to the LLM, and returns a revised driver text validated
against the same structural rules as fresh generation.

The function is the building block for gh#40's Layer 5 eval loop:

    results = run Layer 5 evals                 # ToolEvalResult list
    signals = [RefinementSignal.from_eval_result(r) for r in results
               if r.tool_expected == name]
    text = refine_driver(name, signals)
    write_generated_driver(name, text, overwrite=True)   # → quarantine
    re-run evals; promote_driver(name) if improved

Safety posture mirrors ``generate_driver`` (gh#41 invariants):
- ``_check_tool_name`` fires at the top — unsafe/reserved names fail fast.
- Output must pass ``_validate_driver_text`` before it is returned.
- Refined text carries a provenance header INSIDE the body (after the
  frontmatter close) so ``_parse_driver_frontmatter`` still parses at
  byte 0.
- The refined text is NOT written here; callers route through
  ``write_generated_driver`` so the quarantine flow (scenario #50)
  applies to refined drivers exactly as to fresh ones.
- Failure evidence originates in eval scrollback (untrusted); the prompt
  builder wraps it with the DO-NOT-FOLLOW sentinels (Audit H19).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Optional

from llm import chat, get_client
from prompts import UNTRUSTED_CONTENT_RULE, _DRIVERS_DIR

from .generator import _UNREVIEWED_DRIVERS_DIR, _check_tool_name, _validate_driver_text
from .models import RefinementSignal
from .prompts import build_refinement_prompt

log = logging.getLogger(__name__)

REFINED_HEADER_TEMPLATE = "<!-- Refined by refine_driver on {date} -->"
REFINED_HEADER = "Refined by refine_driver"


def _load_current_driver(
    tool_name: str,
    drivers_dir: Optional[str],
    unreviewed_dir: Optional[str],
) -> str:
    """Load the driver to refine — canonical location wins over quarantine."""
    base = drivers_dir if drivers_dir is not None else _DRIVERS_DIR
    quarantine = unreviewed_dir if unreviewed_dir is not None else _UNREVIEWED_DRIVERS_DIR
    for candidate in (
        os.path.join(base, f"{tool_name}.md"),
        os.path.join(quarantine, f"{tool_name}.md"),
    ):
        if os.path.exists(candidate):
            with open(candidate, "r") as f:
                return f.read()
    raise FileNotFoundError(
        f"no driver to refine for {tool_name!r}: looked in {base} and {quarantine}"
    )


def refine_driver(
    tool_name: str,
    signals: list[RefinementSignal],
    drivers_dir: Optional[str] = None,
    unreviewed_dir: Optional[str] = None,
    client=None,
    model: Optional[str] = None,
) -> str:
    """Refine the driver for ``tool_name`` from eval-failure signals.

    Returns the revised driver markdown (validated, provenance header
    injected inside the body). Does not write — pass the result to
    ``write_generated_driver(..., overwrite=True)`` so the quarantine
    flow applies.

    Raises:
      - ``ValueError`` if ``tool_name`` is unsafe or reserved
      - ``ValueError`` if ``signals`` contains no failure (nothing to
        refine on — refining from passing runs invites hallucinated
        "improvements")
      - ``FileNotFoundError`` if no current driver exists in either the
        canonical or quarantine location
      - ``ValueError`` if the LLM output fails structural validation
    """
    _check_tool_name(tool_name)

    failures = [s for s in signals if s.is_failure]
    if not failures:
        raise ValueError(
            f"refusing to refine driver for {tool_name!r}: no failure "
            f"signals ({len(signals)} passing results) — nothing to fix"
        )

    current = _load_current_driver(tool_name, drivers_dir, unreviewed_dir)

    client = client if client is not None else get_client()
    prompt = build_refinement_prompt(tool_name, current, failures)
    messages = [
        {
            "role": "system",
            "content": (
                "You are clive's tool-discovery driver refiner.\n\n"
                f"{UNTRUSTED_CONTENT_RULE}"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    text, _pt, _ct = chat(client, messages, model=model, max_tokens=1500)
    text = text.strip()

    _validate_driver_text(tool_name, text)
    log.info(
        "refined driver for %s from %d failure signal(s) (of %d results)",
        tool_name, len(failures), len(signals),
    )
    return _inject_refined_header(text)


def _inject_refined_header(text: str) -> str:
    """Insert the provenance header right after the closing frontmatter ---."""
    front_end = text.find("---", 3)
    head = text[: front_end + 3]
    tail = text[front_end + 3:]
    header = REFINED_HEADER_TEMPLATE.format(date=_dt.date.today().isoformat())
    if tail.startswith("\n"):
        return f"{head}\n{header}{tail}"
    return f"{head}\n{header}\n{tail}"
