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
    df = df.copy().sort_values("datetime")
    df["date"] = df["datetime"].dt.date.astype(str)
    df["price"] = df["date"].map(price_map).ffill()

    # --- Vectorized totals (fast, O(n)) instead of per-wallet filtering ---
    bought_totals = df.groupby("to")["value_token"].sum()
    sold_totals = df.groupby("from")["value_token"].sum()
    all_wallets = pd.unique(pd.concat([df["from"], df["to"]]))
    totals = pd.DataFrame({"wallet": all_wallets})
    totals["total_bought"] = totals["wallet"].map(bought_totals).fillna(0.0)
    totals["total_sold"] = totals["wallet"].map(sold_totals).fillna(0.0)
    totals["net_position"] = totals["total_bought"] - totals["total_sold"]

    # --- FIFO realized PnL ---
    # Only wallets that sold at least once can have nonzero realized PnL —
    # for a large accumulation-heavy token, most wallets never sell, so
    # this alone can skip the vast majority of the wallet set. For the
    # rest, split buys/sells into per-wallet groups ONCE (groupby is a
    # single O(n log n) pass) instead of re-scanning the full transfer
    # set for every wallet (which was the real O(wallets x transfers)
    # bottleneck — the reason runs went from ~1 min to 10-30+ min once
    # this function started actually running on tens of thousands of
    # wallets instead of exiting early on empty price data).
    sellers = sold_totals[sold_totals > 0].index
    buy_groups = {w: g[["value_token", "price"]].values for w, g in df[df["to"].isin(sellers)].groupby("to")}
    sell_groups = {w: g[["value_token", "price"]].values for w, g in df[df["from"].isin(sellers)].groupby("from")}

    realized_map = {}
    for w in sellers:
        lots = buy_groups.get(w, [])
        sells = sell_groups.get(w, [])
        realized = 0.0
        li = 0
        remaining_qty, remaining_price = (lots[0][0], lots[0][1]) if len(lots) else (0, 0)
        for sell_qty, sell_price in sells:
            sell_price = 0 if pd.isna(sell_price) else sell_price
            qty_to_sell = sell_qty
            while qty_to_sell > 0 and li < len(lots):
                if remaining_qty <= 0:
                    li += 1
                    if li >= len(lots):
                        break
                    remaining_qty, remaining_price = lots[li][0], lots[li][1]
                    continue
                matched = min(qty_to_sell, remaining_qty)
                realized += matched * (sell_price - remaining_price)
                qty_to_sell -= matched
                remaining_qty -= matched
        realized_map[w] = realized

    totals["realized_pnl_usd"] = totals["wallet"].map(realized_map).fillna(0.0)

    # NOTE: pin the column order/schema explicitly (also handles the
    # 0-wallet edge case cleanly) so downstream code sees a consistent
    # shape regardless of dataset size.
    if totals.empty:
        return pd.DataFrame(columns=PNL_COLUMNS)
    return totals[PNL_COLUMNS]


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

    df = df.sort_values("datetime").copy()
    df["bucket"] = df["datetime"].dt.floor(f"{settings.coordinated_window_minutes}min")

    # Vectorized: compute each row's per-bucket unique-wallet count in one
    # groupby().transform() pass, then select "hot" rows directly — avoids
    # looping over every hot bucket and re-filtering the full dataframe
    # each time (which, with a fine-grained time window over a long
    # history, can mean tens of thousands of full-dataframe re-scans).
    bucket_wallet_counts = df.groupby("bucket")["to"].transform("nunique")
    coordinated_wallets = set(df.loc[bucket_wallet_counts >= settings.coordinated_min_wallets, "to"].unique())

    first_buy_amount = df.groupby("to")["value_token"].first()
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

    top_candidates = pnl_df.sort_values("net_position", ascending=False).head(
        settings.top_n_wallets_deep_analysis
    )["wallet"].tolist()
    top_candidates_set = set(top_candidates)

    # Build each top-candidate's buy history with ONE filter + groupby
    # pass over the full transfer set, instead of re-scanning it once per
    # wallet (42,000 wallets x 310,000 rows was the actual reason runs
    # went from ~1 min to 10-30+ min once this scenario started genuinely
    # processing large wallet counts).
    top_buys_by_wallet = {
        w: g for w, g in df_filtered[df_filtered["to"].isin(top_candidates_set)].groupby("to")
    }

    rows = []
    for w in pnl_df["wallet"]:
        if w in top_candidates_set:
            buys = top_buys_by_wallet.get(w, df_filtered.iloc[0:0])
            timing_score = _accumulation_timing_score(buys, prices)
            funding = _funding_source(w, chain)
        else:
            timing_score = 0.0
            funding = "not_analyzed"
        rows.append({"wallet": w, "timing_score": timing_score, "funding_source": funding})

    # Same fix as _realized_pnl: pin columns so an empty `rows` list still
    # produces a DataFrame with a "wallet" column, so the merge below
    # never raises KeyError: 'wallet'.
    extra_df = pd.DataFrame(rows, columns=EXTRA_COLUMNS)

    result = pnl_df.merge(extra_df, on="wallet", how="left").merge(flags_df, on="wallet", how="left")
    result["flagged_coordinated"] = result["flagged_coordinated"].fillna(False)
    result["flagged_fresh_big_buy"] = result["flagged_fresh_big_buy"].fillna(False)

    # funding cluster size
    cluster_sizes = result[~result["funding_source"].isin(["unknown", "not_analyzed"])].groupby("funding_source")["wallet"].transform("count")
    result["cluster_size"] = cluster_sizes.reindex(result.index).fillna(1)
    result["in_funding_cluster"] = result["cluster_size"] > 1

    # cross-token diversity: how many OTHER fetched tokens has this wallet touched
    all_transfer_files = [
        f for f in data_layer.DATA_RAW_DIR.glob("*_transfers.parquet")
    ]
    diversity_counts = {}
    if len(all_transfer_files) > 1:
        # Build each file's wallet-address set ONCE, then do O(1) set
        # lookups per wallet — instead of re-scanning every other token's
        # full transfer file for every single wallet (wallets x files x
        # rows_per_file), which was extremely slow for large wallet counts.
        wallet_sets_per_file = []
        for f in all_transfer_files:
            try:
                other = pd.read_parquet(f, columns=["from", "to"])
            except Exception:
                continue
            wallet_sets_per_file.append(set(other["from"]) | set(other["to"]))
        for w in result["wallet"]:
            diversity_counts[w] = sum(1 for s in wallet_sets_per_file if w in s)
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
