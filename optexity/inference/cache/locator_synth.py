"""Turn a browser-use trace element into a deterministic Playwright locator command.

A trace element (from ``AgentHistoryList.export_deterministic_trace`` in the browser-use
fork) is a plain dict with ``attributes``, ``node_name`` (tag), ``x_path`` and ``ax_name``.
The optexity engine already ships a battle-tested locator-scoring heuristic in
``LocatorExtraction._scored_candidates`` (test-id > id > name > aria-label > role+name >
placeholder > css/text > xpath). Rather than reinvent that precedence we adapt the trace
element to the duck-typed interface that scorer expects, so cached locators are chosen the
exact same way the engine itself prefers them at runtime.

The emitted command is the ``eval``-able expression the engine runs as ``page.<command>``
(see ``Browser.get_locator_from_command``), e.g. ``locator("[name='01___title']")``.
"""

from __future__ import annotations

import re

from optexity.inference.core.interaction.utils import LocatorExtraction

# A name/id we are willing to trust for a cached locator even when the engine's general
# _looks_dynamic heuristic rejects it. That heuristic flags any token with >=2 digits and
# letters as dynamic (to catch hashes like "3xY7z"), which false-positives on perfectly
# stable form names like "04fullname" / "13adr_city". For a *cached* locator we have an
# extra signal the heuristic lacks: this exact value already resolved to the right element
# at capture time. So we re-admit it when it looks like a hand-authored identifier — a
# short single token of name-safe chars with a modest digit ratio — rather than a hash.
_STABLE_IDENT = re.compile(r"^[A-Za-z0-9_\-:.]{1,40}$")


def _looks_hand_authored(value: str) -> bool:
    if not value or not _STABLE_IDENT.match(value):
        return False
    digits = sum(c.isdigit() for c in value)
    # Hashes are digit-dense; hand-authored identifiers are mostly letters.
    return digits / len(value) <= 0.4


class _AxNode:
    """Minimal stand-in for browser-use's accessibility node: only role + name are read
    by the scorer."""

    def __init__(self, role: str, name: str) -> None:
        self.role = role
        self.name = name


class _TraceElementAdapter:
    """Adapts a trace element dict to the attribute interface ``_scored_candidates``
    reads (``attributes``, ``tag_name``, ``ax_node``, ``xpath``).

    Role is not captured per-element by browser-use's ``DOMInteractedElement`` (only the
    accessible *name* is), so we recover an explicit ``role`` attribute when present and
    otherwise leave it blank — the higher-priority id/name/placeholder candidates carry
    the load for form fields regardless.
    """

    def __init__(self, element: dict) -> None:
        attrs = element.get("attributes") or {}
        self.attributes = attrs
        self.tag_name = (element.get("node_name") or "*").lower()
        self.xpath = element.get("x_path") or ""
        self.ax_node = _AxNode(role=(attrs.get("role") or ""), name=element.get("ax_name") or "")


def ranked_commands(element: dict) -> list[dict]:
    """All viable locator commands for the element, best-first, each tagged with its
    ``kind`` and stability ``score``. Useful for logging/debugging the cache decision."""
    adapter = _TraceElementAdapter(element)
    return [
        {"command": command, "kind": kind, "score": score}
        for score, kind, command in LocatorExtraction._scored_candidates(adapter)
    ]


def _relaxed_attr_command(element: dict) -> dict | None:
    """A name/id/test-id locator re-admitted under the relaxed cache rule, or None.
    Used only when the engine heuristic leaves nothing better than xpath."""
    attrs = element.get("attributes") or {}
    tag = (element.get("node_name") or "*").lower()
    quote = LocatorExtraction._quote_locator_value
    for attr, kind, score in (("data-testid", "test-id", 95), ("id", "id", 90), ("name", "name", 82)):
        val = (attrs.get(attr) or "").strip()
        if val and _looks_hand_authored(val):
            if attr == "id" and re.match(r"^[A-Za-z][\w-]*$", val):
                sel = f"#{val}"
            else:
                sel = LocatorExtraction._css_attr(tag, attr, val)
            return {"command": f"locator({quote(sel, 400)})", "kind": kind, "score": score}
    return None


def synthesize_command(element: dict) -> dict | None:
    """The single best deterministic locator command for a trace element, or ``None`` if
    the element exposes nothing stable enough to locate on. Returns
    ``{"command", "kind", "score"}``.

    Prefers the engine's own ranking; only when that yields nothing better than a
    positional xpath do we fall back to a relaxed name/id selector (more robust to layout
    changes than xpath, and known-good since it resolved at capture time).
    """
    candidates = ranked_commands(element)
    best = candidates[0] if candidates else None
    if best is None or best["kind"] == "xpath":
        relaxed = _relaxed_attr_command(element)
        if relaxed is not None:
            return relaxed
    return best
