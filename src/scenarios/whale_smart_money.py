"""
whale_smart_money.py — Scenario: rank wallets by a weighted
"smart money" score combining:
  1. Exchange-wallet exclusion (heuristic)
  2. Realized PnL (FIFO, using cached daily prices)
  3. Funding-source clustering (detect multi-wallet entities)
  4. Cross-token diversity (bonus signal, best-effort — limited to this
     token's own data unless other tokens have also been fetched)
  5. Accumulation-timing (bought during low-volatility phase vs FOMO)
  6. Coordinated-buying / fresh-wallet-big-buy (weighted, not standalone)
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import data_layer
from config import settings

SCENARIO_NAME = "whale_smart_money"

PNL_COLUMNS = ["wallet", "realized_pnl_usd", "total_bought", "total_sold", "net_position"]
EXTRA_COLUMNS = ["wallet", "timing_score", "funding_source"]


def _detect_exchange_addresses(df: pd.DataFrame, chain: str) -> set:
    known = set(settings.known_exchange_addresses)
    sender_counts = df.groupby("to")["from"].nunique()
    heuristic = set(sender_counts[sender_counts >= settings.exchange_unique_sender_threshold].index)
    detected = known | heuristic

    cache = data_layer.load_exchange_cache()
    now = datetime.now(timezone.utc).isoformat()
    new_rows = pd.DataFrame({
        "address": list(detected),
        "chain": chain,
        "unique_senders": [int(sender_counts.get(a, 0)) for a in detected],
        "detected_at": now,
    })
    cache = pd.concat([cache, new_rows]).drop_duplicates(subset=["address", "chain"], keep="last")
    data_layer.save_exchange_cache(cache)
    return detected


def _realized_pnl(df: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame(columns=PNL_COLUMNS)

    price_map = dict(zip(prices["date"], prices["price_usd"]))
    df = df.copy()
    df["date"] = df["datetime"].dt.date.astype(str)
    df["price"] = df["date"].map(price_map).ffill()

    wallets = pd.unique(pd.concat([df["from"], df["to"]]))
    results = []
    for w in wallets:
        buys = df[df["to"] == w].sort_values("datetime")
        sells = df[df["from"] == w].sort_values("datetime")
        total_bought = buys["value_token"].sum()
        total_sold = sells["value_token"].sum()

        # FIFO match
        lots = list(zip(buys["value_token"], buys["price"].fillna(0)))
        realized = 0.0
        li = 0
        remaining_qty, remaining_price = (lots[0] if lots else (0, 0))
        for _, srow in sells.iterrows():
            qty_to_sell = srow["value_token"]
            sell_price = srow["price"] if not pd.isna(srow["price"]) else 0
            while qty_to_sell > 0 and li < len(lots):
                if remaining_qty <= 0:
                    li += 1
                    if li >= len(lots):
                        break
                    remaining_qty, remaining_price = lots[li]
                    continue
                matched = min(qty_to_sell, remaining_qty)
                realized += matched * (sell_price - remaining_price)
                qty_to_sell -= matched
                remaining_qty -= matched

        results.append({
            "wallet": w,
            "realized_pnl_usd": realized,
            "total_bought": total_bought,
            "total_sold": total_sold,
            "net_position": total_bought - total_sold,
        })

    # NOTE: pd.DataFrame([]) with an empty list produces a DataFrame with
    # NO columns at all, which later breaks `pnl_df["wallet"]` / merges.
    # Always pin the columns explicitly so downstream code sees a
    # consistent (possibly 0-row) schema even when `wallets` is empty.
    return pd.DataFrame(results, columns=PNL_COLUMNS)


def _funding_source(wallet: str, chain: str) -> str:
    """First incoming native-token tx sender = who funded this wallet's gas."""
    cache = data_layer.load_wallet_cache()
    hit = cache[cache["wallet"] == wallet]
    if not hit.empty and pd.notna(hit.iloc[0].get("funding_source")):
        return hit.iloc[0]["funding_source"]
    # Best-effort: without native-tx explorer call wired up here to keep
    # this scenario self-contained on token-transfer data only, we fall
    # back to "unknown" — extend by calling the explorer 'txlist' action
    # per-wallet if you have paid-tier rate limits available.
    return "unknown"


def _accumulation_timing_score(wallet_buys: pd.DataFrame, prices: pd.DataFrame) -> float:
    if wallet_buys.empty or prices.empty:
        return 0.0
    prices_sorted = prices.sort_values("date").copy()
    prices_sorted["pct_change"] = prices_sorted["price_usd"].pct_change().abs()
    vol_map = dict(zip(prices_sorted["date"], prices_sorted["pct_change"]))

    dates = wallet_buys["datetime"].dt.date.astype(str)
    vols = dates.map(vol_map).dropna()
    if vols.empty:
        return 0.0
    avg_vol_on_buy = vols.mean()
    overall_avg_vol = prices_sorted["pct_change"].mean()
    if not overall_avg_vol or np.isnan(overall_avg_vol):
        return 0.0
    # lower volatility-on-buy relative to overall average => higher score
    ratio = avg_vol_on_buy / overall_avg_vol if overall_avg_vol else 1
    return float(max(0, min(1, 1 - ratio)))


def _coordinated_and_fresh_flags(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["wallet", "flagged_coordinated", "flagged_fresh_big_buy"])

    df = df.sort_values("datetime")
    coordinated_wallets = set()

    buys = df.copy()
    buys["bucket"] = buys["datetime"].dt.floor(f"{settings.coordinated_window_minutes}min")
    grouped = buys.groupby("bucket")["to"].nunique()
    hot_buckets = grouped[grouped >= settings.coordinated_min_wallets].index
    for b in hot_buckets:
        coordinated_wallets.update(buys[buys["bucket"] == b]["to"].unique())

    first_buy_amount = df.sort_values("datetime").groupby("to").first()["value_token"]
    fresh_big_buy = set(first_buy_amount[first_buy_amount >= settings.fresh_wallet_buy_threshold].index)

    return pd.DataFrame({
        "wallet": list(set(df["to"].unique())),
    }).assign(
        flagged_coordinated=lambda d: d["wallet"].isin(coordinated_wallets),
        flagged_fresh_big_buy=lambda d: d["wallet"].isin(fresh_big_buy),
    )


def run(df: pd.DataFrame, prices: pd.DataFrame, chain: str = "ethereum", **kwargs) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    exchange_addrs = _detect_exchange_addresses(df, chain)

    # IMPORTANT: do NOT drop rows involving exchange_addrs here.
    # For DEX-traded tokens (Uniswap etc.), almost every buy/sell shows up
    # as a transfer to/from the liquidity-pool address — that transfer IS
    # the trade. Dropping those rows removes the actual buy/sell signal
    # entirely and leaves nothing to score (this was the root cause of
    # whale_smart_money returning 0 rows for pool-traded tokens).
    # Exchange/pool addresses are excluded later, only from the final
    # ranked wallet list — so the pool never shows up as a "whale wallet"
    # itself, but every real wallet's trades through it are still counted.
    df_filtered = df

    pnl_df = _realized_pnl(df_filtered, prices)
    flags_df = _coordinated_and_fresh_flags(df_filtered)

    if pnl_df.empty:
        # Nothing survived the exchange filter / no priced transfers —
        # return an empty, correctly-shaped result instead of crashing.
        return pd.DataFrame(columns=[
            "wallet", "smart_money_score", "realized_pnl_usd", "total_bought", "total_sold",
            "net_position", "distinct_tokens_traded", "timing_score", "funding_source",
            "in_funding_cluster", "cluster_size", "flagged_coordinated", "flagged_fresh_big_buy",
        ])

    rows = []
    top_candidates = pnl_df.sort_values("net_position", ascending=False).head(
        settings.top_n_wallets_deep_analysis
    )["wallet"].tolist()

    for w in pnl_df["wallet"]:
        buys = df_filtered[df_filtered["to"] == w]
        timing_score = _accumulation_timing_score(buys, prices) if w in top_candidates else 0.0
        funding = _funding_source(w, chain) if w in top_candidates else "not_analyzed"
        rows.append({"wallet": w, "timing_score": timing_score, "funding_source": funding})

    # Same fix as _realized_pnl: pin columns so an empty `rows` list still
    # produces a DataFrame with a "wallet" column, so the merge below
    # never raises KeyError: 'wallet'.
    extra_df = pd.DataFrame(rows, columns=EXTRA_COLUMNS)

    result = pnl_df.merge(extra_df, on="wallet", how="left").merge(flags_df, on="wallet", how="left")
    result["flagged_coordinated"] = result["flagged_coordinated"].fillna(False)
    result["flagged_fresh_big_buy"] = result["flagged_fresh_big_buy"].fillna(False)

    # funding cluster size
    cluster_sizes = result[result["funding_source"] != "unknown"].groupby("funding_source")["wallet"].transform("count")
    result["cluster_size"] = cluster_sizes.reindex(result.index).fillna(1)
    result["in_funding_cluster"] = result["cluster_size"] > 1

    # cross-token diversity: how many OTHER fetched tokens has this wallet touched
    all_transfer_files = list(data_layer.DATA_RAW_DIR.glob("*_transfers.parquet"))
    diversity_counts = {}
    if len(all_transfer_files) > 1:
        for w in result["wallet"]:
            count = 0
            for f in all_transfer_files:
                try:
                    other = pd.read_parquet(f, columns=["from", "to"])
                except Exception:
                    continue
                if (other["to"] == w).any() or (other["from"] == w).any():
                    count += 1
            diversity_counts[w] = count
    result["distinct_tokens_traded"] = result["wallet"].map(diversity_counts).fillna(1)

    # normalize sub-scores 0-1 for scoring
    def _norm(s):
        rng = s.max() - s.min()
        return (s - s.min()) / rng if rng else s * 0

    pnl_norm = _norm(result["realized_pnl_usd"].clip(lower=0))
    accum_norm = _norm(result["net_position"].clip(lower=0))
    diversity_norm = _norm(result["distinct_tokens_traded"])
    timing_norm = result["timing_score"].fillna(0)
    coord_fresh_norm = (result["flagged_coordinated"].astype(int) + result["flagged_fresh_big_buy"].astype(int)) / 2

    result["smart_money_score"] = (
        settings.weight_pnl * pnl_norm
        + settings.weight_accumulation * accum_norm
        + settings.weight_cross_token * diversity_norm
        + settings.weight_timing * timing_norm
        + settings.weight_coordinated_fresh * coord_fresh_norm
    ).round(4)

    result = result[result["net_position"].notna()]
    result = result[~result["wallet"].isin(exchange_addrs)]  # pool/exchange itself shouldn't be ranked as a "wallet"
    result = result[result["net_position"] >= settings.whale_threshold_tokens / 10]  # keep meaningfully-sized wallets
    result = result.sort_values("smart_money_score", ascending=False).reset_index(drop=True)

    cols = [
        "wallet", "smart_money_score", "realized_pnl_usd", "total_bought", "total_sold",
        "net_position", "distinct_tokens_traded", "timing_score", "funding_source",
        "in_funding_cluster", "cluster_size", "flagged_coordinated", "flagged_fresh_big_buy",
    ]
    return result[cols]
