"""
data_layer.py — the ONLY place that talks to the explorer API / CoinGecko
and the ONLY place that reads/writes parquet files.

Every scenario script calls functions from here instead of touching
files or APIs directly. This is what makes "fetch once, run many
scenarios" possible.
"""

import json
import os
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
    """
    Convert YYYY-MM-DD into the nearest block number using
    the Etherscan V2 getblocknobytime endpoint.
    """

    ts = int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )

    url = settings.explorer_base_url(chain)

    params = {
        "chainid": settings.chain_id(chain),   # <-- Added
        "module": "block",
        "action": "getblocknobytime",
        "timestamp": ts,
        "closest": "before",
        "apikey": settings.explorer_api_key,
    }

    resp = requests.get(url, params=params, timeout=30)
    data = resp.json()

    if data.get("status") != "1":
        raise ValueError(
            f"Could not resolve block for date {date_str}: {data.get('result')}"
        )

    return int(data["result"])


# ------------------------------------------------------------------ fetch --

def fetch_transfers(
    chain: str,
    contract: str,
    from_block: int = None,
    to_block: int = None,
    from_date: str = None,
    to_date: str = None,
    force_full: bool = False,
) -> pd.DataFrame:
    """
    Fetch ERC-20 transfer events.

    Incremental by default.
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

    # Etherscan (and BscScan/PolygonScan/Arbiscan) enforce a hard limit:
    # page * offset must stay <= 10000. Going past it doesn't return an
    # empty list — it returns an error payload (result is a string, not
    # a list), which used to be silently treated as "no more data" and
    # capped every fetch at exactly 10,000 rows total.
    #
    # Fix: once a page comes back FULL (== offset rows) and the NEXT
    # page would cross that 10,000 ceiling, don't increment the page —
    # instead re-anchor startblock to (last block seen + 1) and reset
    # page back to 1. This walks forward in successive <=10k windows,
    # so total history of any size can be fetched, not just the first 10k.
    current_from_block = from_block
    max_block_seen = from_block - 1 if from_block > 0 else 0
    windows_fetched = 0
    MAX_WINDOWS = 2000  # safety valve (covers up to ~20M transfers)

    while True:

        params = {
            "chainid": settings.chain_id(chain),
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract,
            "startblock": current_from_block,
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

        last_block_in_page = int(rows[-1]["blockNumber"])
        max_block_seen = max(max_block_seen, last_block_in_page)

        if len(rows) < offset:
            # partial page = genuinely reached the end of available data
            break

        if (page + 1) * offset > 10000:
            # would cross Etherscan's page*offset cap — re-anchor instead
            current_from_block = last_block_in_page + 1
            page = 1
        else:
            page += 1

        windows_fetched += 1
        time.sleep(REQUEST_PAUSE_SECONDS)

        if windows_fetched > MAX_WINDOWS:
            break

    new_df = pd.DataFrame(all_rows)

    out_path = transfers_path(chain, contract)

    if not new_df.empty:

        new_df = new_df[
            [
                "hash",
                "blockNumber",
                "timeStamp",
                "from",
                "to",
                "value",
                "tokenDecimal",
                "tokenSymbol",
            ]
        ].copy()

        new_df["blockNumber"] = new_df["blockNumber"].astype(int)
        new_df["timeStamp"] = new_df["timeStamp"].astype(int)
        new_df["tokenDecimal"] = new_df["tokenDecimal"].astype(int)

        new_df["value_token"] = new_df.apply(
            lambda r: int(r["value"]) / (10 ** r["tokenDecimal"]),
            axis=1,
        )

        new_df["from"] = new_df["from"].str.lower()
        new_df["to"] = new_df["to"].str.lower()

        new_df["datetime"] = pd.to_datetime(
            new_df["timeStamp"],
            unit="s",
            utc=True,
        )

        if out_path.exists() and not force_full:

            existing = pd.read_parquet(out_path)

            combined = (
                pd.concat([existing, new_df])
                .drop_duplicates(
                    subset=["hash", "from", "to", "value"]
                )
            )

        else:
            combined = new_df

        combined.to_parquet(out_path, index=False)

    else:

        if out_path.exists():
            combined = pd.read_parquet(out_path)
        else:
            combined = pd.DataFrame()

    state[key] = {
        "last_block": max_block_seen,
        "last_synced": datetime.now(timezone.utc).isoformat(),
    }

    _save_sync_state(state)

    return combined


def load_transfers(
    chain: str,
    contract: str,
    from_date: str = None,
    to_date: str = None,
) -> pd.DataFrame:

    p = transfers_path(chain, contract)

    if not p.exists():
        raise FileNotFoundError(
            f"No raw data found for {chain}/{contract}. "
            f"Run with --fetch first."
        )

    df = pd.read_parquet(p)

    if from_date:
        df = df[df["datetime"] >= pd.Timestamp(from_date, tz="utc")]

    if to_date:
        df = df[
            df["datetime"]
            <= pd.Timestamp(to_date, tz="utc") + pd.Timedelta(days=1)
        ]

    return df.reset_index(drop=True)


# ------------------------------------------------------------------ price --

def fetch_prices(
    chain: str,
    contract: str,
    coin_id: str = None,
) -> pd.DataFrame:

    contract = contract.lower()

    out_path = prices_path(chain, contract)

    existing = (
        pd.read_parquet(out_path)
        if out_path.exists()
        else pd.DataFrame(columns=["date", "price_usd"])
    )

    platform = settings.coingecko_platform(chain)

    headers = {}
    api_key = os.getenv("COINGECKO_API_KEY", "").strip()  # strip stray whitespace/newlines from secret paste
    if api_key:
        # Demo-plan keys accept the key via header OR query param. We send
        # BOTH — some network layers (proxies, certain runners) can strip
        # custom headers, and CoinGecko's own docs lead with the query-
        # param form, so this is the most compatibility-safe approach.
        headers["x-cg-demo-api-key"] = api_key

    def _get(url, params):
        """GET with retry-with-backoff on 429 (rate limit) / 5xx.
        Returns the final response (may still be non-200)."""
        if api_key:
            params = {**params, "x_cg_demo_api_key": api_key}
        last_resp = None
        for attempt in range(4):
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            last_resp = resp
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
                continue
            break  # non-retryable (e.g. 404) — no point retrying
        return last_resp

    contract_url = (
        f"https://api.coingecko.com/api/v3/coins/"
        f"{platform}/contract/{contract}/market_chart"
    )
    # CoinGecko's free/Demo plan caps historical lookback at 365 days
    # (days="max" is Pro-only and returns error_code 10012 "exceeds the
    # allowed time range" on Demo/keyless requests).
    params = {"vs_currency": "usd", "days": "365", "interval": "daily"}

    resp = _get(contract_url, params)

    if resp is None or resp.status_code != 200:
        status = resp.status_code if resp is not None else "no response"
        body = resp.text[:200] if resp is not None else ""
        print(
            f"[price-fetch] response body: {body}"
        )
        print(
            f"[price-fetch] WARNING: contract-lookup failed for "
            f"{chain}/{contract} (HTTP {status}). "
            f"Common causes: (1) CoinGecko rate-limit on keyless/shared-IP "
            f"requests (set COINGECKO_API_KEY in .env / GitHub secret to "
            f"fix), or (2) this token migrated/rebranded to a new contract "
            f"and CoinGecko hasn't mapped it yet — set COINGECKO_COIN_ID in "
            f".env to fetch by coin id instead. Falling back to any "
            f"previously-cached price data ({len(existing)} rows)."
        )
        # Fallback: fetch by coin id if one was given (handles rebrand/
        # migration cases where the contract-address lookup 404s but the
        # coin itself is listed under a stable id, e.g. "anyone-protocol")
        fallback_id = coin_id or os.getenv("COINGECKO_COIN_ID", "")
        if fallback_id:
            id_url = f"https://api.coingecko.com/api/v3/coins/{fallback_id}/market_chart"
            resp = _get(id_url, params)
            if resp is None or resp.status_code != 200:
                status = resp.status_code if resp is not None else "no response"
                print(f"[price-fetch] WARNING: coin-id fallback '{fallback_id}' also failed (HTTP {status}).")
                return existing
            print(f"[price-fetch] coin-id fallback '{fallback_id}' succeeded.")
        else:
            return existing

    data = resp.json()

    prices = data.get("prices", [])

    if not prices:
        print(f"[price-fetch] WARNING: CoinGecko returned HTTP 200 but no price points for {chain}/{contract}.")
        return existing

    new_df = pd.DataFrame(
        prices,
        columns=["ts_ms", "price_usd"],
    )

    new_df["date"] = (
        pd.to_datetime(new_df["ts_ms"], unit="ms", utc=True)
        .dt.date.astype(str)
    )

    new_df = (
        new_df.groupby("date", as_index=False)["price_usd"]
        .last()
    )

    combined = (
        pd.concat([existing, new_df])
        .drop_duplicates(subset=["date"], keep="last")
    )

    combined.to_parquet(out_path, index=False)
    print(f"[price-fetch] OK — {len(combined)} daily price rows cached for {chain}/{contract}.")

    return combined


def load_prices(chain: str, contract: str) -> pd.DataFrame:

    p = prices_path(chain, contract)

    if p.exists():
        return pd.read_parquet(p)

    return pd.DataFrame(columns=["date", "price_usd"])


# --------------------------------------------------------- wallet caches --

def load_wallet_cache() -> pd.DataFrame:

    p = wallet_cache_path()

    if p.exists():
        return pd.read_parquet(p)

    return pd.DataFrame(
        columns=[
            "wallet",
            "funding_source",
            "distinct_tokens_traded",
            "cached_at",
        ]
    )


def save_wallet_cache(df: pd.DataFrame):
    df.to_parquet(wallet_cache_path(), index=False)


def load_exchange_cache() -> pd.DataFrame:

    p = exchange_cache_path()

    if p.exists():
        return pd.read_parquet(p)

    return pd.DataFrame(
        columns=[
            "address",
            "chain",
            "unique_senders",
            "detected_at",
        ]
    )


def save_exchange_cache(df: pd.DataFrame):
    df.to_parquet(exchange_cache_path(), index=False)
