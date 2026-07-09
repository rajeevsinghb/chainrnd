"""
exchange_flow.py — Scenario: track known/detected exchange wallets'
daily net inflow vs outflow (market-wide sentiment signal), plus
large single-transaction deposit/withdrawal alerts.
"""
import pandas as pd

import data_layer
from config import settings

SCENARIO_NAME = "exchange_flow"


def _exchange_addresses(df: pd.DataFrame, chain: str) -> set:
    known = set(settings.known_exchange_addresses)
    cached = data_layer.load_exchange_cache()
    cached_for_chain = set(cached[cached["chain"] == chain]["address"]) if not cached.empty else set()
    if known or cached_for_chain:
        return known | cached_for_chain

    # fallback: run the same heuristic used in whale_smart_money if no cache exists yet
    sender_counts = df.groupby("to")["from"].nunique()
    return set(sender_counts[sender_counts >= settings.exchange_unique_sender_threshold].index)


def run(df: pd.DataFrame, prices: pd.DataFrame, chain: str = "ethereum", **kwargs) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    exchanges = _exchange_addresses(df, chain)
    if not exchanges:
        return pd.DataFrame({"note": ["No exchange addresses known/detected yet. "
                                       "Run whale_smart_money scenario first, or set "
                                       "KNOWN_EXCHANGE_ADDRESSES in .env"]})

    df = df.copy()
    df["date"] = df["datetime"].dt.date.astype(str)
    df["is_inflow"] = df["to"].isin(exchanges) & ~df["from"].isin(exchanges)
    df["is_outflow"] = df["from"].isin(exchanges) & ~df["to"].isin(exchanges)

    daily = df.groupby("date").apply(
        lambda g: pd.Series({
            "inflow_tokens": g.loc[g["is_inflow"], "value_token"].sum(),
            "outflow_tokens": g.loc[g["is_outflow"], "value_token"].sum(),
        })
    ).reset_index()
    daily["net_flow_tokens"] = daily["inflow_tokens"] - daily["outflow_tokens"]
    daily["signal"] = daily["net_flow_tokens"].apply(
        lambda x: "bearish_pressure (net inflow to exchanges)" if x > 0
        else ("bullish_pressure (net outflow from exchanges)" if x < 0 else "neutral")
    )

    return daily.sort_values("date").reset_index(drop=True)


def large_flow_alerts(df: pd.DataFrame, chain: str = "ethereum") -> pd.DataFrame:
    exchanges = _exchange_addresses(df, chain)
    if not exchanges:
        return pd.DataFrame()
    df = df.copy()
    df["is_inflow"] = df["to"].isin(exchanges) & ~df["from"].isin(exchanges)
    df["is_outflow"] = df["from"].isin(exchanges) & ~df["to"].isin(exchanges)
    big = df[(df["is_inflow"] | df["is_outflow"]) & (df["value_token"] >= settings.large_flow_alert_threshold)]
    big = big.copy()
    big["direction"] = big["is_inflow"].map({True: "deposit_to_exchange", False: "withdrawal_from_exchange"})
    return big[["hash", "datetime", "from", "to", "value_token", "direction"]].sort_values(
        "datetime", ascending=False
    ).reset_index(drop=True)
