#!/usr/bin/env python3
"""
run.py — single entry point for the whole project.

Examples
--------
Fetch only (no analysis):
    python run.py --fetch --chain ethereum --contract 0xABC...

Fetch a specific date range only:
    python run.py --fetch --chain ethereum --contract 0xABC... \\
        --from-date 2026-01-01 --to-date 2026-03-01

Run one scenario on already-fetched data (no new fetch):
    python run.py --scenario whale_smart_money --chain ethereum --contract 0xABC...

Run multiple scenarios at once:
    python run.py --scenario whale_smart_money,exchange_flow --chain ethereum --contract 0xABC...

Fetch AND analyze in one go:
    python run.py --fetch --scenario whale_smart_money --chain ethereum --contract 0xABC...

Analyze only a specific window of already-fetched data:
    python run.py --scenario exchange_flow --chain ethereum --contract 0xABC... \\
        --from-date 2026-06-01 --to-date 2026-06-30

Discovery:
    python run.py --list-scenarios
    python run.py --list-raw-data
    python run.py --list-results
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import data_layer
import scenarios as scenario_registry
from config import settings, OUTPUT_DIR


def cmd_list_scenarios():
    names = scenario_registry.list_scenarios()
    print("Available scenarios:")
    for n in names:
        print(f"  - {n}")


def cmd_list_raw_data():
    rows = data_layer.list_fetched_datasets()
    if not rows:
        print("No raw data fetched yet.")
        return
    print(f"{'chain':<10} {'contract':<44} {'rows':>8}  last_block   last_synced")
    for r in rows:
        print(f"{r['chain']:<10} {r['contract']:<44} {r['rows']:>8}  {r['last_block']}   {r['last_synced']}")


def cmd_list_results():
    if not OUTPUT_DIR.exists():
        print("No results yet.")
        return
    files = sorted(OUTPUT_DIR.rglob("*.csv"))
    if not files:
        print("No results yet.")
        return
    for f in files:
        print(f.relative_to(OUTPUT_DIR.parent))


def _save_output(df: pd.DataFrame, chain: str, contract: str, scenario_name: str):
    out_dir = OUTPUT_DIR / f"{chain}_{contract.lower()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{scenario_name}_{ts}.csv"
    df.to_csv(out_path, index=False)

    latest_path = out_dir / f"{scenario_name}_latest.csv"
    df.to_csv(latest_path, index=False)
    print(f"  -> saved {out_path.relative_to(OUTPUT_DIR.parent)}")
    print(f"  -> saved {latest_path.relative_to(OUTPUT_DIR.parent)} (always overwritten with newest run)")


def main():
    parser = argparse.ArgumentParser(description="chainrnd — flexible on-chain fetch + scenario runner")
    parser.add_argument("--fetch", action="store_true", help="Fetch/refresh raw transfer + price data")
    parser.add_argument("--scenario", type=str, default=None,
                         help="Comma-separated scenario name(s) to run, e.g. whale_smart_money,exchange_flow")
    parser.add_argument("--chain", type=str, default="ethereum",
                         choices=["ethereum", "bsc", "polygon", "arbitrum"])
    parser.add_argument("--contract", type=str, default=None, help="Token contract address")
    parser.add_argument("--from-block", type=int, default=None)
    parser.add_argument("--to-block", type=int, default=None)
    parser.add_argument("--from-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--to-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--force-full", action="store_true", help="Ignore incremental cache, refetch everything")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--list-raw-data", action="store_true")
    parser.add_argument("--list-results", action="store_true")
    args = parser.parse_args()

    if args.list_scenarios:
        cmd_list_scenarios()
        return
    if args.list_raw_data:
        cmd_list_raw_data()
        return
    if args.list_results:
        cmd_list_results()
        return

    if not args.contract and (args.fetch or args.scenario):
        parser.error("--contract is required for --fetch / --scenario")

    if args.fetch:
        print(f"[fetch] {args.chain} / {args.contract} ...")
        data_layer.fetch_transfers(
            chain=args.chain, contract=args.contract,
            from_block=args.from_block, to_block=args.to_block,
            from_date=args.from_date, to_date=args.to_date,
            force_full=args.force_full,
        )
        data_layer.fetch_prices(chain=args.chain, contract=args.contract)
        print("[fetch] done.")

    if args.scenario:
        df = data_layer.load_transfers(args.chain, args.contract,
                                        from_date=args.from_date, to_date=args.to_date)
        prices = data_layer.load_prices(args.chain, args.contract)
        print(f"[analyze] {len(df)} transfers loaded for {args.chain}/{args.contract}")

        for name in [s.strip() for s in args.scenario.split(",") if s.strip()]:
            module = scenario_registry.get_scenario(name)
            print(f"[scenario] running '{name}' ...")
            result = module.run(df, prices, chain=args.chain, contract=args.contract)
            if result is None or result.empty:
                print(f"  (no output rows for '{name}')")
                continue
            _save_output(result, args.chain, args.contract, name)

            if name == "exchange_flow" and hasattr(module, "large_flow_alerts"):
                alerts = module.large_flow_alerts(df, chain=args.chain)
                if not alerts.empty:
                    _save_output(alerts, args.chain, args.contract, "exchange_flow_alerts")

    if not (args.fetch or args.scenario):
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
