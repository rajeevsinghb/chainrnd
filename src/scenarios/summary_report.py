"""
summary_report.py — Scenario: builds ONE Markdown report combining the
whale_smart_money + exchange_flow results, with embedded charts, so the
full picture is visible from a single file on GitHub instead of having
to open several separate CSVs.

Covers 4 things:
  1. Quick stats (transfers, wallets, date range, counts)
  2. Top whale/smart-money wallets (table + score-distribution chart)
  3. Exchange flow trend (chart + current bullish/bearish signal)
  4. Recent large-flow alerts (table)

Run alongside the other two scenarios:
    --scenario whale_smart_money,exchange_flow,summary_report

Unlike the other scenarios, this one writes its own files directly
(a markdown report + PNG charts) instead of returning a single tabular
result to be saved as CSV — it always returns an empty DataFrame so
run.py's normal CSV-save step is a no-op for it.

Output:
    output/<chain>_<contract>/summary_latest.md
    output/<chain>_<contract>/charts/*.png
"""
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")  # headless — no display available on GitHub Actions runners
import matplotlib.pyplot as plt
import pandas as pd

import scenarios.exchange_flow as ef
import scenarios.whale_smart_money as wsm
from config import OUTPUT_DIR

SCENARIO_NAME = "summary_report"


def _short(wallet: str) -> str:
    return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet


def run(df: pd.DataFrame, prices: pd.DataFrame, chain: str = "ethereum", contract: str = "", **kwargs) -> pd.DataFrame:
    if df.empty or not contract:
        return pd.DataFrame()

    out_dir = OUTPUT_DIR / f"{chain}_{contract.lower()}"
    charts_dir = out_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    whale_df = wsm.run(df, prices, chain=chain)
    flow_df = ef.run(df, prices, chain=chain)
    alerts_df = ef.large_flow_alerts(df, chain=chain) if hasattr(ef, "large_flow_alerts") else pd.DataFrame()
    # exchange_flow.run() can also return a 1-row "note" dataframe (no
    # exchange addresses known yet) instead of the real daily-flow shape —
    # detect that case so the trend section below doesn't crash on it.
    if "note" in flow_df.columns:
        flow_df = pd.DataFrame()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append("# chainrnd Summary Report")
    lines.append(f"**Token:** `{contract}` on **{chain}**  ")
    lines.append(f"**Generated:** {generated_at}")
    lines.append("")

    # --- 1. Quick stats ---
    lines.append("## Quick Stats")
    lines.append("")
    unique_wallets = pd.unique(pd.concat([df["from"], df["to"]])).shape[0]
    lines.append(f"- Total transfers analyzed: **{len(df):,}**")
    lines.append(f"- Unique wallets seen: **{unique_wallets:,}**")
    lines.append(f"- Date range: **{df['datetime'].min().date()}** to **{df['datetime'].max().date()}**")
    lines.append(f"- Whale/smart-money wallets ranked: **{len(whale_df):,}**")
    lines.append(f"- Days of exchange-flow data: **{len(flow_df):,}**")
    lines.append(f"- Large-flow alerts: **{len(alerts_df):,}**")
    lines.append("")

    # --- 2. Top whale/smart-money wallets ---
    lines.append("## Top Whale / Smart-Money Wallets")
    lines.append("")
    if whale_df.empty:
        lines.append("_No wallets met the ranking threshold this run._")
    else:
        top = whale_df.head(15)
        lines.append("| # | Wallet | Score | Realized PnL (USD) | Net Position | Coordinated? | Fresh Big Buy? |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, row in enumerate(top.itertuples(), 1):
            lines.append(
                f"| {i} | `{_short(row.wallet)}` | {row.smart_money_score:.4f} | "
                f"${row.realized_pnl_usd:,.0f} | {row.net_position:,.0f} | "
                f"{'Yes' if row.flagged_coordinated else 'No'} | "
                f"{'Yes' if row.flagged_fresh_big_buy else 'No'} |"
            )

        fig, ax = plt.subplots(figsize=(7, 3.5))
        ax.hist(whale_df["smart_money_score"], bins=30, color="#4C72B0", edgecolor="white")
        ax.set_xlabel("Smart Money Score")
        ax.set_ylabel("Wallet Count")
        ax.set_title("Smart Money Score Distribution")
        fig.tight_layout()
        fig.savefig(charts_dir / "score_distribution.png", dpi=120)
        plt.close(fig)
        lines.append("")
        lines.append("![Score distribution](charts/score_distribution.png)")
    lines.append("")

    # --- 3. Exchange flow trend ---
    lines.append("## Exchange Flow Trend")
    lines.append("")
    if flow_df.empty:
        lines.append("_No exchange-flow data this run (no exchange addresses detected yet — "
                      "run whale_smart_money first, or set KNOWN_EXCHANGE_ADDRESSES)._")
    else:
        flow_sorted = flow_df.sort_values("date")
        recent = flow_sorted.tail(90)  # last ~90 days, keeps the chart readable
        current_signal = flow_sorted.iloc[-1]["signal"]
        lines.append(f"**Current signal (most recent day):** {current_signal}")
        lines.append("")

        dates = pd.to_datetime(recent["date"])
        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot(dates, recent["net_flow_tokens"], color="#333333", linewidth=1)
        ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
        ax.fill_between(dates, recent["net_flow_tokens"], 0,
                         where=(recent["net_flow_tokens"] >= 0), color="#C44E52", alpha=0.4,
                         interpolate=True, label="Net inflow (bearish)")
        ax.fill_between(dates, recent["net_flow_tokens"], 0,
                         where=(recent["net_flow_tokens"] < 0), color="#55A868", alpha=0.4,
                         interpolate=True, label="Net outflow (bullish)")
        ax.set_ylabel("Net Flow (tokens)")
        ax.set_title(f"Exchange Net Flow — Last {len(recent)} Days")
        ax.legend(loc="upper left", fontsize=8)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(charts_dir / "exchange_flow_trend.png", dpi=120)
        plt.close(fig)
        lines.append("![Exchange flow trend](charts/exchange_flow_trend.png)")
    lines.append("")

    # --- 4. Recent large-flow alerts ---
    lines.append("## Recent Large-Flow Alerts")
    lines.append("")
    if alerts_df.empty:
        lines.append("_No large-flow alerts this run._")
    else:
        recent_alerts = alerts_df.sort_values("datetime", ascending=False).head(10)
        lines.append("| Date | Direction | Amount (tokens) |")
        lines.append("|---|---|---|")
        for row in recent_alerts.itertuples():
            lines.append(f"| {row.datetime} | {row.direction} | {row.value_token:,.0f} |")
    lines.append("")

    report_path = out_dir / "summary_latest.md"
    report_path.write_text("\n".join(lines))
    n_charts = len(list(charts_dir.glob("*.png")))
    print(f"[summary] wrote {report_path} ({n_charts} chart(s))")

    # This scenario writes its own files directly (report + charts) —
    # returning an empty frame means run.py's normal per-scenario CSV
    # save is a no-op here (by design, not an error).
    return pd.DataFrame()
