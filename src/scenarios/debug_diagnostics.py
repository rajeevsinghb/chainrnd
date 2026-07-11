"""
debug_diagnostics.py — TEMPORARY diagnostic scenario.

Run this once via:
    --scenario debug_diagnostics
(no --fetch needed, uses already-fetched data)

It reports, in one CSV row, every number needed to pinpoint exactly
WHERE whale_smart_money's pipeline is losing all its rows:
  - how many transfers/wallets were loaded
  - how many price rows were fetched from CoinGecko (0 = price fetch
    failed for this token, which alone would explain an empty result)
  - how many addresses the exchange-detection heuristic flagged
  - how many wallets survive each filter step, in order

Delete this file once the real issue is found — it's not part of the
permanent scenario set.
"""
import pandas as pd

import scenarios.whale_smart_money as wsm
from config import settings

SCENARIO_NAME = "debug_diagnostics"


def run(df: pd.DataFrame, prices: pd.DataFrame, chain: str = "ethereum", **kwargs) -> pd.DataFrame:
    stats = {}

    stats["total_transfers"] = len(df)
    stats["unique_wallets"] = int(pd.unique(pd.concat([df["from"], df["to"]])).shape[0]) if not df.empty else 0
    stats["price_rows_fetched"] = len(prices)
    stats["price_date_min"] = prices["date"].min() if not prices.empty else None
    stats["price_date_max"] = prices["date"].max() if not prices.empty else None

    exchange_addrs = wsm._detect_exchange_addresses(df, chain)
    stats["exchange_addresses_detected"] = len(exchange_addrs)
    stats["exchange_addresses_sample"] = ", ".join(list(exchange_addrs)[:10])

    pnl_df = wsm._realized_pnl(df, prices)
    stats["wallets_after_pnl_calc"] = len(pnl_df)

    if not pnl_df.empty:
        stats["max_net_position"] = float(pnl_df["net_position"].max())
        stats["median_net_position"] = float(pnl_df["net_position"].median())
        stats["wallets_net_position_gt_0"] = int((pnl_df["net_position"] > 0).sum())

        after_exchange_exclusion = pnl_df[~pnl_df["wallet"].isin(exchange_addrs)]
        stats["wallets_after_excluding_exchange_addrs"] = len(after_exchange_exclusion)

        threshold = settings.whale_threshold_tokens / 10
        stats["whale_threshold_used"] = threshold
        after_threshold = after_exchange_exclusion[after_exchange_exclusion["net_position"] >= threshold]
        stats["wallets_after_threshold_filter"] = len(after_threshold)
    else:
        stats["max_net_position"] = None
        stats["median_net_position"] = None
        stats["wallets_net_position_gt_0"] = 0
        stats["wallets_after_excluding_exchange_addrs"] = 0
        stats["whale_threshold_used"] = settings.whale_threshold_tokens / 10
        stats["wallets_after_threshold_filter"] = 0

    return pd.DataFrame([stats])
