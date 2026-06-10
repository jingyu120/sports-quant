# QuantSlate

A deterministic MLB +EV betting pipeline. It pulls real sabermetric inputs and live bookmaker odds, projects each game with a run-expectancy model, computes expected value (EV) and fractional-Kelly stakes for every market, and prints a ranked slate of the best bets. **No LLM is involved** — the output is fully reproducible for a given set of inputs.

## Quick start

```bash
npm install
cp .env.example .env        # add your ODDS_API_KEY (see below)
npm run generate            # fetches data, runs the model, prints the slate
```

`npm run generate` runs both stages and writes:
- `quantslate_payload.json` — the per-game bets with all computed metrics
- `quantslate_slate.md` — the final globally-ranked top-N table + exposure summary (also printed to console)

## How it works

The pipeline is three stages; **all analysis finishes in stage 2**, so the output is just sorted, formatted data.

```
fetch_metrics.py ──▶ real_sabermetrics.json ──▶ quantslate-ingest.ts ──▶ quantslate_payload.json
   (MLB Stats API)      (team profiles)            (The Odds API +            quantslate_slate.md
                                                    run model + EV/Kelly)      (ranked table)
```

### Stage 1 — `fetch_metrics.py`
Builds `real_sabermetrics.json` for the teams playing on the target date, sourced live from the public MLB Stats API:

- **Probable starter** — the actual listed starter for each game, with handedness.
- **Starter FIP** — individual FIP, computed with a **league-calibrated FIP constant** (derived from that season's real league totals) and **regressed toward the league mean by innings pitched** (small samples get pulled in). Used as the run-prevention skill input. *Note: without batted-ball data a true SIERA can't be computed, so the regressed FIP fills the `SIERA` field.*
- **Platoon offense** — real team wRC+ proxy vs LHP / vs RHP from actual hitting splits, relative to the live league split average.
- **Bullpen fatigue** — real trailing-3-day reliever pitch counts, summed from completed-game boxscores.
- **Park factors** — a maintained static table (these are stable and not exposed by the API).

Every network call falls back to a league-average value, so one missing data point never aborts the slate.

Run for a specific date: `QUANTSLATE_DATE=2025-06-09 python fetch_metrics.py` (defaults to today).

### Stage 2 — `quantslate-ingest.ts`
1. Fetches live decimal odds (h2h / spreads / totals) from [The Odds API](https://the-odds-api.com/) for the target bookmakers.
2. Merges them with the stage-1 sabermetrics and projects runs per team (`calculateProjectedRuns`): starter-vs-bullpen SIERA weighting, wRC+ platoon factor, bullpen-fatigue factor, dampened park factor, and a home-field bump.
3. Converts projections to win/total/spread probabilities (Pythagorean win expectancy for moneylines; a normal-CDF approximation for totals and spreads).
4. **Shrinks the model probability 30% model / 70% market** toward bookmaker-implied probability (de-vigged) to respect market efficiency.
5. Computes `EV = model_prob × best_decimal_odds − 1` and a quarter-Kelly stake for each market, keeping only bets above the EV floor.
6. Writes `quantslate_payload.json` and renders `quantslate_slate.md` — the global top-N table plus a real exposure summary (total bankroll deployed, largest stake, and a correlated-position warning for same-game bets).

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `ODDS_API_KEY` | — | **Required.** Your The Odds API key (set in `.env`). |
| `MIN_EV` | `0.03` | Minimum EV edge to surface a bet (decimal). Lower = more, thinner bets; higher = fewer, higher-conviction. This is your margin of safety against model error — see note below. |
| `MAX_BETS` | `10` | Max rows in the ranked slate table. |
| `QUANTSLATE_DATE` | today | Target slate date (`YYYY-MM-DD`) for `fetch_metrics.py`. |

Examples:
```bash
MIN_EV=0.05 npm run generate     # only 5%+ edges
MAX_BETS=15 npm run generate     # show up to 15 bets
```

## Reading the output — important caveats

- **`MIN_EV` is a safety margin, not a quality dial.** `model_prob` is an estimate built on imperfect inputs; a displayed +2% edge can be negative if the model is off by a point or two. The threshold only surfaces edges large enough to likely survive your own estimation error. Below ~1.5% you're betting noise/vig. An empty slate means "no edge today" — a valid result, not a bug.
- **Correlated bets.** Multiple bets in the same matchup (e.g. a team's ML and its +1.5) are **not independent** — Kelly assumes independence, so summing their stakes overstates your true edge. The exposure summary flags same-game clusters and reports a **correlation-capped** figure (largest stake per game only); treat real risk as between the capped and total numbers.
- **The numbers are only as good as the model.** The run model is a reasonable approximation, not a market-beating engine. Use it as a screen for candidates, not blind instruction.

## Files

| File | Role |
|---|---|
| `fetch_metrics.py` | Stage 1 — scrapes live sabermetric inputs → `real_sabermetrics.json` |
| `quantslate-ingest.ts` | Stage 2 — odds + model + EV/Kelly → payload + ranked slate |
| `types.ts` | Shared TypeScript types for the Odds API and sabermetric profiles |
| `real_sabermetrics.json` | Generated team profiles (regenerated each run) |
| `quantslate_payload.json` | Generated per-game bets with computed metrics |
| `quantslate_slate.md` | Generated final ranked table + exposure summary |
