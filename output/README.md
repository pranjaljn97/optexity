# Cache outputs — deterministic automations, one per task

Each file is a **deterministic automation compiled by the learning cache** (see
`optexity/inference/cache/`). Values come from real captured runs, not hand-authoring.

| File | Site | Seed | What it shows |
|------|------|------|---------------|
| `roboform_cached.json` | roboform "all fields" | `test_automation.json` | agentic form-fill → 4 deterministic `input_text` nodes with `[name=…]` locators |
| `download_cached.json` | the-internet.herokuapp.com/download | `test_automation_download.json` | agentic click-to-download → 1 deterministic `click_element`. The link's only signals were a **random filename**, so the cache correctly falls back to a stable positional xpath instead of the dynamic text |
| `flights_cached.json` | Google Flights | _(recorded server automation)_ | a real 18-node recorded automation after **self-repair**: a stale locator (`get_by_role("combobox", name="Where else?")`) was relearned to `get_by_label("Where to?")` |
| `hyperliquid_cached.json` | Hyperliquid | _(recorded server automation)_ | a real 8-node recorded automation after **self-repair**: a stale asset-selector text locator (`get_by_text("S&P500-USDC")`, the displayed asset had changed) was relearned to a positional xpath |
| `orangehrm_buzz_cached.json` | OrangeHRM Buzz | `test_automation_orangehrm_buzz.json` | a multi-step agentic flow with **no-verification login** compiled to 9 deterministic nodes: log in (`Admin`/`admin123` via `[name=…]`) → open Buzz → **post** (`What's on your mind?`) → **like** → **comment** (`Write your comment...`). The comment's Enter-to-submit was a keyboard action (not element-bound), so it isn't a cached node — the cached flow types the comment but doesn't press Enter |

> **Output JSON vs. runtime cache.** These `output/` files are the *deliverable* snapshots to
> show. The engine's live, per-endpoint store is `../optexity_cache/<endpoint>.json` — written
> during a run and read back on the next run to skip the LLM. It is normally a runtime artifact,
> but the two real-endpoint entries (`search_google_flights-…`, `hyperliquid_initiate_trade-…`)
> are checked in here for demonstration of the keyed per-endpoint cache. For roboform/download
> the output is an agentic trace **compiled** to deterministic nodes; for flights/hyperliquid
> (already-deterministic recorded automations) it is the **self-healed** automation.

All five pass `Automation.model_validate`.

## Measured caching benefit (agentic vs. cached replay)

| Task | Agentic tokens | Cached tokens | Reduction | Cached fallbacks |
|------|---------------:|--------------:|----------:|------------------|
| roboform | 25,339 | **0** | 100% | 0 / 4 nodes |
| OrangeHRM Buzz | 99,356 | **6,900** | **93%** | 2 / 9 nodes |

- **roboform** (static form): every step replays from a cached locator → **0 LLM tokens, 0 fallbacks**.
- **OrangeHRM Buzz** (login + post + like + comment): the static parts (whole **login** + **post**)
  replay deterministically; only the two genuinely **dynamic** feed interactions — the *like*
  (positional xpath on a feed whose posts changed) and the *comment* (its box appears only after
  the comment-icon click) — fall back to the LLM. Net **93% fewer tokens** (99,356 → 6,900). With
  `OPTEXITY_CACHE_HEAL` on, those two re-cache each run, though a constantly-changing feed will
  keep needing the occasional fallback (correct behavior).

> Tokens are the clean signal; wall-clock improves less on these runs because it's dominated by
> the engine's fixed per-node `end_sleep_time` and browser startup, not LLM latency.

Regenerate any of these from a captured trace:

```bash
python -m optexity.inference.cache.cli compile \
    --logs /tmp/optexity/<task_id>/logs --url <url> --out output/<task>_cached.json
```
