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

from optexity.inference.core.interaction.utils import LocatorExtraction


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


def synthesize_command(element: dict) -> dict | None:
    """The single best deterministic locator command for a trace element, or ``None`` if
    the element exposes nothing stable enough to locate on. Returns
    ``{"command", "kind", "score"}``."""
    candidates = ranked_commands(element)
    return candidates[0] if candidates else None
