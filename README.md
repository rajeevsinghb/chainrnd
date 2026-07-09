# chainrnd

Fully-flexible on-chain fetch + scenario runner. Fetch raw transfer data for
any token on any supported chain, cache it as Parquet, and run any number of
pluggable "scenarios" (analyses) against it — independently, repeatedly, and
without ever re-fetching data you already have.

## What this does

```
FETCH (explorer API)  -->  STORE (Parquet, incremental)  -->  ANALYZE (scenarios)  -->  OUTPUT (CSV)
```

- **Fetch** and **Analyze** are fully decoupled. You can:
  - Fetch only, and analyze later (0 scenarios)
  - Fetch + run 1 scenario
  - Fetch + run multiple scenarios at once
  - Skip fetching entirely and just re-run a scenario on data you already have
  - Filter either the fetch OR the analysis to a specific date/block range
- Raw data is cached as **Parquet** (`data/raw/`) — a run only fetches the
  blocks it doesn't already have (incremental sync via `data/cache/sync_state.json`).
- Results are written as **CSV** (`output/<chain>_<contract>/<scenario>_<timestamp>.csv`,
  plus an always-current `<scenario>_latest.csv`).
- New analyses = new files. Add a file in `src/scenarios/`, nothing else changes.

## Included scenarios (Phase 0)

| Scenario | What it does |
|---|---|
| `whale_smart_money` | Ranks wallets by a weighted score combining realized PnL (FIFO), exchange-wallet exclusion, funding-source clustering, cross-token diversity, accumulation timing, and coordinated/fresh-wallet flags |
| `exchange_flow` | Tracks daily net inflow/outflow for known/detected exchange wallets (market sentiment) + large single-transaction deposit/withdrawal alerts |

## Project structure

```
chainrnd/
├── .github/workflows/run.yml   GitHub Actions — run everything without a local machine
├── data/
│   ├── raw/                    Parquet: transfers + prices per chain/contract
│   └── cache/                  sync_state.json, wallet + exchange-address caches
├── output/                     Final CSV results (per chain/contract, per scenario)
├── src/
│   ├── config.py                All settings (from .env / GitHub Secrets)
│   ├── data_layer.py            The ONLY place that calls APIs or touches parquet
│   ├── run.py                   CLI entry point
│   └── scenarios/
│       ├── whale_smart_money.py
│       └── exchange_flow.py
├── .env.example
├── .gitignore
└── requirements.txt
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: add your EXPLORER_API_KEY (free at etherscan.io/myapikey)
```

## Usage (CLI)

All commands run from the `src/` folder.

```bash
cd src

# Fetch only — no analysis
python run.py --fetch --chain ethereum --contract 0xTokenAddress

# Fetch a specific date range only
python run.py --fetch --chain ethereum --contract 0xTokenAddress \
  --from-date 2026-01-01 --to-date 2026-03-01

# Run a scenario on data already fetched (no new fetch)
python run.py --scenario whale_smart_money --chain ethereum --contract 0xTokenAddress

# Run multiple scenarios at once
python run.py --scenario whale_smart_money,exchange_flow --chain ethereum --contract 0xTokenAddress

# Fetch AND analyze in one command
python run.py --fetch --scenario whale_smart_money --chain ethereum --contract 0xTokenAddress

# Analyze only a specific window of already-fetched data (no fetch, fast)
python run.py --scenario exchange_flow --chain ethereum --contract 0xTokenAddress \
  --from-date 2026-06-01 --to-date 2026-06-30

# Discovery
python run.py --list-scenarios
python run.py --list-raw-data
python run.py --list-results
```

Recommended first run on a new token: run `whale_smart_money` before
`exchange_flow` — it populates the exchange-address cache that
`exchange_flow` reuses (no heuristic re-run needed).

## Usage (GitHub Actions — no local machine needed)

1. Push this repo to GitHub as-is.
2. Add your API key as a **secret** (not in `.env`, never committed):
   `Settings -> Secrets and variables -> Actions -> New repository secret`
   `Name: EXPLORER_API_KEY`
3. (Optional) Add known exchange addresses as a **variable**:
   `Settings -> Secrets and variables -> Actions -> Variables -> New repository variable`
   `Name: KNOWN_EXCHANGE_ADDRESSES`, value comma-separated addresses.
4. Go to the **Actions** tab -> "Run chainrnd" -> **Run workflow**, fill in
   `mode` (fetch / scenario / both), `chain`, `contract`, `scenario`,
   optional date range -> Run.
5. When it finishes, the workflow **commits** the updated `data/raw`,
   `data/cache`, and `output/` files back into the repo — so results and
   raw data are both visible and downloadable directly from GitHub, and the
   next run resumes from where this one left off (no re-fetching).

## Adding a new scenario later

Create `src/scenarios/my_new_scenario.py`:

```python
SCENARIO_NAME = "my_new_scenario"

def run(df, prices, chain="ethereum", **kwargs):
    # df = all cached transfers for the selected chain/contract (already
    #      filtered to any --from-date/--to-date you passed on the CLI)
    # prices = cached daily USD price history
    # return a pandas DataFrame — it will be saved to output/ as CSV
    ...
    return result_df
```

Nothing else needs to change — `run.py --list-scenarios` will pick it up
automatically.

## Honest limitations (read before trusting the numbers)

- PnL uses **daily** close prices, not intra-day — treat it as approximate,
  not exact.
- Exchange-address detection is **heuristic** (unique-sender count +
  optional manual list), not a verified label database — false
  positives/negatives are possible.
- Funding-source clustering only uses data already present in the fetched
  transfer set; it is not a full multi-signal entity-resolution system.
- This is a personal research/signal tool, not investment advice, and not
  a replacement for commercial platforms (Nansen, Arkham, Chainalysis)
  which rely on verified wallet-label databases, real-time infrastructure,
  and teams maintaining them continuously.
