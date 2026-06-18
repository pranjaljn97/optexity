"""Self-repairing cache: fingerprint verification + heal-on-fallback.

Two opt-in capabilities that make the deterministic cache correct under upstream drift,
kept in this single module so the engine stays clean and the feature is trivially
removable. The engine calls out here from a handful of 2-3 line guarded hooks; with the
toggles off (the default) those hooks short-circuit and behavior is unchanged.

- **verify** (`verify_or_fallback`): before acting on a cached locator, confirm the live
  element still matches the element the command was compiled against. Mismatch ⇒ the
  engine falls back to the LLM instead of acting on the wrong element.
- **heal** (`record_heal` + `persist_healed_automation`): when the LLM fallback relearns
  an element, write its fresh locator + fingerprint back into the node and persist the
  repaired automation, so the cache stops re-paying the LLM for that step.

Toggle via env: OPTEXITY_CACHE_VERIFY=1, OPTEXITY_CACHE_HEAL=1, OPTEXITY_CACHE_SERVE=1.
Cache entries are keyed per endpoint under OPTEXITY_CACHE_DIR (default ``optexity_cache``);
set OPTEXITY_CACHE_HEAL_PATH to force a single shared file instead.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from optexity.schema.actions.interaction_action import ElementFingerprint

logger = logging.getLogger(__name__)

# Attributes that establish element *identity*. A strict match on these (plus tag) is what
# distinguishes "same element" from "locator now points somewhere else".
IDENTITY_ATTRS = ("id", "name", "data-testid", "aria-label", "placeholder")
# Identity attrs plus a couple of descriptive ones kept for context/debugging.
FINGERPRINT_ATTRS = IDENTITY_ATTRS + ("type", "role")

# JS read of the same attribute subset off a live Playwright locator's element.
_LIVE_SIGNATURE_JS = """
el => {
  const a = {};
  for (const k of ['id','name','data-testid','aria-label','placeholder','type','role']) {
    const v = el.getAttribute ? el.getAttribute(k) : null;
    if (v != null && v !== '') a[k] = v;
  }
  return { tag: (el.tagName || '').toLowerCase(), attributes: a };
}
"""


class CacheConfig:
    """Feature toggles, read once from the environment. Defaults OFF (opt-in)."""

    def __init__(self) -> None:
        self.verify_fingerprint = os.getenv("OPTEXITY_CACHE_VERIFY", "0") == "1"
        self.heal_on_fallback = os.getenv("OPTEXITY_CACHE_HEAL", "0") == "1"
        # Serve a previously cached deterministic automation for an endpoint instead of the
        # server's (and auto-populate the cache after an agentic run). This is what makes
        # subsequent runs actually *reuse* the cache.
        self.serve_cache = os.getenv("OPTEXITY_CACHE_SERVE", "0") == "1"
        # Cache is keyed by endpoint_name: one entry per endpoint under cache_dir, so many
        # endpoints coexist without overwriting each other (capacity is bounded only by
        # disk — each entry is a few KB). Set OPTEXITY_CACHE_HEAL_PATH to force a single
        # fixed file instead (back-compat / single-automation iteration).
        self.cache_dir = os.getenv("OPTEXITY_CACHE_DIR", "optexity_cache")
        self.heal_path = os.getenv("OPTEXITY_CACHE_HEAL_PATH") or None


cache_config = CacheConfig()


def get_cache_config() -> CacheConfig:
    """Accessor (lets tests reassign the module singleton)."""
    return cache_config


def _safe_key(endpoint_name: str | None) -> str:
    """Filesystem-safe cache key from an endpoint name."""
    name = (endpoint_name or "default").strip() or "default"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)


def cached_path_for(endpoint_name: str | None) -> str:
    """Per-endpoint cache file path: ``<cache_dir>/<endpoint_name>.json``. If a single
    fixed ``heal_path`` is configured, that wins (all endpoints share it)."""
    if cache_config.heal_path:
        return cache_config.heal_path
    return os.path.join(cache_config.cache_dir, f"{_safe_key(endpoint_name)}.json")


def load_cached_automation(endpoint_name: str | None) -> dict | None:
    """Load a previously persisted (compiled/healed) automation for an endpoint, or None.
    Lets the store be looked up by key — the read side of a multi-endpoint cache."""
    path = cached_path_for(endpoint_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load cached automation {path}: {type(e).__name__}: {e}")
        return None


# --------------------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------------------
def make_fingerprint(tag: str | None, attributes: dict | None, ax_name: str | None) -> dict:
    """Build a fingerprint dict from raw element fields (works for both the compile-time
    trace element and a live browser-use element)."""
    attrs = attributes or {}
    return {
        "tag": (tag or "").lower() or None,
        "attributes": {k: attrs[k] for k in FINGERPRINT_ATTRS if attrs.get(k)},
        "ax_name": ax_name or None,
    }


def fingerprint_matches(captured: ElementFingerprint, live: dict) -> bool:
    """Strict match: the tag must match and every identity attribute present at capture
    must still be equal on the live element. Any divergence ⇒ False ⇒ caller falls back."""
    if captured is None:
        return True
    live_attrs = live.get("attributes") or {}
    if captured.tag and live.get("tag") and captured.tag.lower() != live["tag"].lower():
        return False
    for attr in IDENTITY_ATTRS:
        want = captured.attributes.get(attr)
        if want and live_attrs.get(attr) != want:
            return False
    return True


async def live_element_signature(locator) -> dict | None:
    """Read the identity-attribute signature off a resolved Playwright locator. Returns
    None if it can't be read (caller then can't disprove identity → allows the action)."""
    try:
        return await locator.evaluate(_LIVE_SIGNATURE_JS)
    except Exception as e:
        logger.debug(f"live_element_signature failed: {type(e).__name__}: {e}")
        return None


async def verify_or_fallback(action, locator) -> str | None:
    """Verify the live element matches the action's captured fingerprint. Returns an error
    string on mismatch (caller should fall back to the LLM), else None. Never raises —
    an unreadable element returns None (cannot disprove identity, so allow)."""
    live = await live_element_signature(locator)
    if live is None:
        return None
    if fingerprint_matches(action.fingerprint, live):
        return None
    logger.info(
        f"cache fingerprint mismatch: captured={action.fingerprint.model_dump(exclude_none=True)} "
        f"live={live} -> falling back to LLM (not acting on possibly-wrong element)"
    )
    return "fingerprint mismatch"


# --------------------------------------------------------------------------------------
# Heal-on-fallback
# --------------------------------------------------------------------------------------
def record_heal(action, heal_info: dict | None, memory) -> None:
    """Write a relearned locator+fingerprint back into the node and log the heal.

    ``heal_info`` (from the fallback path) is ``{"command", "fingerprint"}`` for the
    element the LLM just acted on. ``action`` is a live reference into ``task.automation``,
    so updating it heals the rest of this run too; the heal is also appended to
    ``memory.heals`` for end-of-run persistence."""
    if not heal_info or not heal_info.get("command"):
        return
    old_command = action.command
    if heal_info["command"] == old_command:
        return  # locator unchanged, nothing to heal
    action.command = heal_info["command"]
    action.skip_command = False
    if heal_info.get("fingerprint"):
        action.fingerprint = ElementFingerprint(**heal_info["fingerprint"])
    step_index = getattr(memory.automation_state, "step_index", None)
    memory.heals.append(
        {
            "step_index": step_index,
            "old_command": old_command,
            "new_command": heal_info["command"],
            "new_fingerprint": heal_info.get("fingerprint"),
        }
    )
    logger.info(f"cache heal: step {step_index} command {old_command!r} -> {heal_info['command']!r}")


def persist_automation(endpoint_name: str | None, automation, path: str | None = None) -> str | None:
    """Write a compiled/deterministic automation to its per-endpoint cache entry (the
    write side of the learning cache; used to seed the cache after an agentic run). Returns
    the path. Best-effort, never raises."""
    out = path or cached_path_for(endpoint_name)
    try:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(automation.model_dump(exclude_none=True), f, indent=2)
        logger.info(f"cache: stored deterministic automation for {endpoint_name} -> {out}")
        return out
    except Exception as e:
        logger.warning(f"Failed to store cached automation: {type(e).__name__}: {e}")
        return None


def persist_healed_automation(task, memory, path: str | None = None) -> str | None:
    """If any heals occurred, write the repaired automation to its per-endpoint cache entry
    and return the path. Keyed by ``task.endpoint_name`` (``<cache_dir>/<endpoint>.json``)
    so many endpoints coexist without overwriting — unless a single fixed ``heal_path`` /
    ``path`` is given. Local-file persistence only. Best-effort, never raises."""
    if not memory.heals:
        return None
    out = path or cached_path_for(getattr(task, "endpoint_name", None))
    try:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(task.automation.model_dump(exclude_none=True), f, indent=2)
        logger.info(f"cache self-repair: persisted {len(memory.heals)} healed node(s) to {out}")
        return out
    except Exception as e:
        logger.warning(f"Failed to persist healed automation: {type(e).__name__}: {e}")
        return None
