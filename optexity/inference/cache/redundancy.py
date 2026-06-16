"""Collapse a raw browser-use trace into the minimal set of deterministic steps.

While exploring, browser-use takes redundant actions: it scrolls and reads the DOM
(pure perception), it sometimes types into the wrong field and corrects it, or retries
the same click. None of that belongs in a compiled automation. This module reduces a
trace to "what actually needed to happen, once":

- drop non-cacheable steps (perception/navigation, failures — already flagged at capture)
- keep only the *last* successful write per (action-family, element): supersedes
  explore-then-correct re-types
- collapse repeated clicks on the same element to a single click

Ordering is preserved by each element's *first* appearance, so fields stay in natural
top-to-bottom order and a final submit click (a distinct, later-first-seen element) stays
last.
"""

from __future__ import annotations

import json

# browser-use action name -> normalized family (input/input_text and click/click_element
# are the same operation under different registry names across versions).
_FAMILY = {
    "input": "input",
    "input_text": "input",
    "click": "click",
    "click_element": "click",
    "select_dropdown": "select",
    "upload_file": "upload",
}


def _element_key(element: dict) -> tuple:
    """A stable identity for a DOM element across steps. Prefer the class-filtered
    ``stable_hash``, fall back to xpath, then to a normalized attribute signature."""
    if element.get("stable_hash") is not None:
        return ("hash", element["stable_hash"])
    if element.get("x_path"):
        return ("xpath", element["x_path"])
    return ("attrs", json.dumps(element.get("attributes") or {}, sort_keys=True))


def collapse_trace(trace: list[dict]) -> list[dict]:
    """Reduce a raw trace to an ordered list of deterministic steps to compile.

    Each returned step is the original trace step augmented with ``family`` (normalized
    action type). Position is set by first appearance; value/goal are taken from the last
    occurrence so corrections win.
    """
    # key -> index into `ordered`, so we can update-in-place while preserving position.
    seen: dict[tuple, int] = {}
    ordered: list[dict] = []

    for step in trace:
        if not step.get("cacheable"):
            continue
        element = step.get("element")
        if not element:
            continue
        family = _FAMILY.get(step["action"])
        if family is None:
            continue

        key = (family, _element_key(element))
        enriched = {**step, "family": family}

        if key in seen:
            # Same operation on the same element seen before:
            # - input: latest value/goal wins (explore-then-correct), keep position
            # - click/upload: idempotent, collapse to the first occurrence
            if family == "input":
                ordered[seen[key]] = enriched
        else:
            seen[key] = len(ordered)
            ordered.append(enriched)

    return ordered
