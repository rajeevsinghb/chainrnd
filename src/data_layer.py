"""
data_layer.py — the ONLY place that talks to the explorer API / CoinGecko
and the ONLY place that reads/writes parquet files.

Every scenario script calls functions from here instead of touching
files or APIs directly. This is what makes "fetch once, run many
scenarios" possible.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from config import settings, DATA_RAW_DIR, DATA_CACHE_DIR

REQUEST_PAUSE_SECONDS = 0.22  # keeps us under 5 req/sec free-tier limits


# ---------------------------------------------------------------- paths ----

def _slug(chain: str, contract: str) -> str:
    return f"{chain.lower()}_{contract.lower()}"


def transfers_path(chain: str, contract: str) -> Path:
    return DATA_RAW_DIR / f"{_slug(chain, contract)}_transfers.parquet"


def prices_path(chain: str, contract: str) -> Path:
    return DATA_RAW_DIR / f"{_slug(chain, contract)}_prices.parquet"


def sync_state_path() -> Path:
    return DATA_CACHE_DIR / "sync_state.json"


def wallet_cache_path() -> Path:
    return DATA_CACHE_DIR / "wallet_metadata.parquet"


def exchange_cache_path() -> Path:
    return DATA_CACHE_DIR / "exchange_addresses.parquet"


# ------------------------------------------------------------ sync state ---

def _load_sync_state() -> dict:
    p = sync_state_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_sync_state(state: dict):
    sync_state_path().write_text(json.dumps(state, indent=2))


def list_fetched_datasets() -> list:
    """Return list of (chain, contract, last_block, rows) already fetched."""
    state = _load_sync_state()
    results = []
    for key, meta in state.items():
        chain, contract = key.split("::")
        p = transfers_path(chain, contract)
        rows = 0
        if p.exists():
            rows = len(pd.read_parquet(p, columns=["hash"]))
        results.append({
            "chain": chain,
            "contract": contract,
            "last_block": meta.get("last_block"),
            "last_synced": meta.get("last_synced"),
            "rows": rows,
        })
    return results


# ---------------------------------------------------------------- date -> block --

def _date_to_block(chain: str, date_str: str) -> int:
    """Convert an ISO date (YYYY-MM-DD) to the nearest block number using
    the explorer's getblocknobytime endpoint."""
    ts = int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    url = settings.explorer_base_url(chain)
    resp = requests.get(url, params={
        "module": "block",
        "action": "getblocknobytime",
        "timestamp": ts,
        "closest": "before",
        "apikey": settings.explorer_api_key,
    }, timeout=30)
    data = resp.json()
    if data.get("status") != "1":
        raise ValueError(f"Could not resolve block for date {date_str}: {data.get('result')}")
    return int(data["result"])


# ------------------------------------------------------------------ fetch --

def fetch_transfers(chain: str, contract: str, from_block: int = None,
                     to_block: int = None, from_date: str = None,
                     to_date: str = None, force_full: bool = False) -> pd.DataFrame:
    """Fetch ERC-20 transfer events for `contract` on `chain`.

    Incremental by default: resumes from the last synced block for this
    chain+contract unless an explicit from_block/from_date or force_full
    is given. Appends new rows to the existing parquet file and returns
    the FULL updated dataframe.
    """
    settings.validate_for_fetch()
    contract = contract.lower()
    key = f"{chain}::{contract}"
    state = _load_sync_state()

    if from_date:
        from_block = _date_to_block(chain, from_date)
    if to_date:
        to_block = _date_to_block(chain, to_date)

    if not force_full and from_block is None:
        from_block = state.get(key, {}).get("last_block")
        if from_block is not None:
            from_block = int(from_block) + 1
    if from_block is None:
        from_block = 0
    if to_block is None:
        to_block = 999_999_999

    url = settings.explorer_base_url(chain)
    all_rows = []
    page = 1
    offset = 10000
    max_block_seen = from_block - 1 if from_block > 0 else 0

    while True:
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "startblock": from_block,
            "endblock": to_block,
            "page": page,
            "offset": offset,
            "sort": "asc",
            "apikey": settings.explorer_api_key,
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        rows = data.get("result", [])
        if not isinstance(rows, list) or not rows:
            break
        all_rows.extend(rows)
        max_block_seen = max(max_block_seen, int(rows[-1]["blockNumber"]))
        if len(rows) < offset:
            break
        page += 1
        time.sleep(REQUEST_PAUSE_SECONDS)
        if page > 500:  # safety valve against runaway loops
            break

    new_df = pd.DataFrame(all_rows)
    out_path = transfers_path(chain, contract)

    if not new_df.empty:
        new_df = new_df[[
            "hash", "blockNumber", "timeStamp", "from", "to", "value",
            "tokenDecimal", "tokenSymbol",
        ]].copy()
        new_df["blockNumber"] = new_df["blockNumber"].astype(int)
        new_df["timeStamp"] = new_df["timeStamp"].astype(int)
        new_df["tokenDecimal"] = new_df["tokenDecimal"].astype(int)
        new_df["value_token"] = new_df.apply(
            lambda r: int(r["value"]) / (10 ** r["tokenDecimal"]), axis=1
        )
        new_df["from"] = new_df["from"].str.lower()
        new_df["to"] = new_df["to"].str.lower()
        new_df["datetime"] = pd.to_datetime(new_df["timeStamp"], unit="s", utc=True)

        if out_path.exists() and not force_full:
            existing = pd.read_parquet(out_path)
            combined = pd.concat([existing, new_df]).drop_duplicates(subset=["hash", "from", "to", "value"])
        else:
            combined = new_df
        combined.to_parquet(out_path, index=False)
    else:
        combined = pd.read_parquet(out_path) if out_path.exists() else pd.DataFrame()

    state[key] = {
        "last_block": max_block_seen,
        "last_synced": datetime.now(timezone.utc).isoformat(),
    }
    _save_sync_state(state)
    return combined


def load_transfers(chain: str, contract: str, from_date: str = None, to_date: str = None) -> pd.DataFrame:
    """Read already-fetched transfers from parquet. Raises if nothing fetched yet."""
    p = transfers_path(chain, contract)
    if not p.exists():
        raise FileNotFoundError(
            f"No raw data found for {chain}/{contract}. Run with --fetch first, "
            f"e.g.  python run.py --fetch --chain {chain} --contract {contract}"
        )
    df = pd.read_parquet(p)
    if from_date:
        df = df[df["datetime"] >= pd.Timestamp(from_date, tz="utc")]
    if to_date:
        df = df[df["datetime"] <= pd.Timestamp(to_date, tz="utc") + pd.Timedelta(days=1)]
    return df.reset_index(drop=True)


# ------------------------------------------------------------------ price --

def fetch_prices(chain: str, contract: str, coin_id: str = None) -> pd.DataFrame:
    """Fetch/refresh daily historical price for the token from CoinGecko,
    caching to parquet so repeat runs only fetch missing dates."""
    contract = contract.lower()
    out_path = prices_path(chain, contract)
    existing = pd.read_parquet(out_path) if out_path.exists() else pd.DataFrame(columns=["date", "price_usd"])

    platform = settings.coingecko_platform(chain)
    url = f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{contract}/market_chart"
    resp = requests.get(url, params={"vs_currency": "usd", "days": "max", "interval": "daily"}, timeout=30)
    if resp.status_code != 200:
        return existing
    data = resp.json()
    prices = data.get("prices", [])
    if not prices:
        return existing

    new_df = pd.DataFrame(prices, columns=["ts_ms", "price_usd"])
    new_df["date"] = pd.to_datetime(new_df["ts_ms"], unit="ms", utc=True).dt.date.astype(str)
    new_df = new_df.groupby("date", as_index=False)["price_usd"].last()

    combined = pd.concat([existing, new_df]).drop_duplicates(subset=["date"], keep="last")
    combined.to_parquet(out_path, index=False)
    return combined


def load_prices(chain: str, contract: str) -> pd.DataFrame:
    p = prices_path(chain, contract)
    if p.exists():
        return pd.read_parquet(p)
    return pd.DataFrame(columns=["date", "price_usd"])


# --------------------------------------------------------- wallet caches ---

def load_wallet_cache() -> pd.DataFrame:
    p = wallet_cache_path()
    if p.exists():
        return pd.read_parquet(p)
    return pd.DataFrame(columns=["wallet", "funding_source", "distinct_tokens_traded", "cached_at"])


def save_wallet_cache(df: pd.DataFrame):
    df.to_parquet(wallet_cache_path(), index=False)


def load_exchange_cache() -> pd.DataFrame:
    p = exchange_cache_path()
    if p.exists():
        return pd.read_parquet(p)
    return pd.DataFrame(columns=["address", "chain", "unique_senders", "detected_at"])


def save_exchange_cache(df: pd.DataFrame):
    df.to_parquet(exchange_cache_path(), index=False)
