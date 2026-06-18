# Cache outputs — deterministic automations, one per task

Each file is a **deterministic automation compiled by the learning cache** (see
`optexity/inference/cache/`). Values come from real captured runs, not hand-authoring.

| File | Site | Seed | What it shows |
|------|------|------|---------------|
| `roboform_cached.json` | roboform "all fields" | `test_automation.json` | agentic form-fill → 4 deterministic `input_text` nodes with `[name=…]` locators |
| `download_cached.json` | the-internet.herokuapp.com/download | `test_automation_download.json` | agentic click-to-download → 1 deterministic `click_element`. The link's only signals were a **random filename**, so the cache correctly falls back to a stable positional xpath instead of the dynamic text |
| `flights_cached.json` | Google Flights | _(recorded server automation)_ | a real 18-node recorded automation after **self-repair**: a stale locator (`get_by_role("combobox", name="Where else?")`) was relearned to `get_by_label("Where to?")` |
| `hyperliquid_cached.json` | Hyperliquid | _(recorded server automation)_ | a real 8-node recorded automation after **self-repair**: a stale asset-selector text locator (`get_by_text("S&P500-USDC")`, the displayed asset had changed) was relearned to a positional xpath |

> **Output JSON vs. runtime cache.** These `output/` files are the *deliverable* snapshots to
> show. The engine's live, per-endpoint store is `../optexity_cache/<endpoint>.json` — written
> during a run and read back on the next run to skip the LLM. It is normally a runtime artifact,
> but the two real-endpoint entries (`search_google_flights-…`, `hyperliquid_initiate_trade-…`)
> are checked in here for demonstration of the keyed per-endpoint cache. For roboform/download
> the output is an agentic trace **compiled** to deterministic nodes; for flights/hyperliquid
> (already-deterministic recorded automations) it is the **self-healed** automation.

All four pass `Automation.model_validate`. roboform was measured at ~25.3k agentic tokens →
**0 tokens / 0 fallbacks** on deterministic replay.

Regenerate any of these from a captured trace:

```bash
python -m optexity.inference.cache.cli compile \
    --logs /tmp/optexity/<task_id>/logs --url <url> --out output/<task>_cached.json
```
