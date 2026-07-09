"""
config.py — single source of truth for all settings.

Everything is read from environment variables (.env locally, or
GitHub Actions Secrets/Variables when run in CI). No file elsewhere
in this project should hardcode a threshold, key, or path.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = ROOT_DIR / "data" / "raw"
DATA_CACHE_DIR = ROOT_DIR / "data" / "cache"
OUTPUT_DIR = ROOT_DIR / "output"

# ============================================================================
# Explorer API (Etherscan V2)
# ============================================================================

EXPLORER_V2_BASE_URL = "https://api.etherscan.io/v2/api"

CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
}

# ============================================================================
# CoinGecko Platform Mapping
# ============================================================================

CHAIN_COINGECKO_PLATFORM = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
}


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


@dataclass
class Settings:
    # =========================================================================
    # API Keys
    # =========================================================================

    explorer_api_key: str = field(
        default_factory=lambda: os.getenv("EXPLORER_API_KEY", "")
    )

    # =========================================================================
    # Analysis Thresholds
    # =========================================================================

    whale_threshold_tokens: float = field(
        default_factory=lambda: _get_float("WHALE_THRESHOLD_TOKENS", 100000)
    )

    accumulation_min_buys: int = field(
        default_factory=lambda: _get_int("ACCUMULATION_MIN_BUYS", 4)
    )

    accumulation_window_days: int = field(
        default_factory=lambda: _get_int("ACCUMULATION_WINDOW_DAYS", 7)
    )

    coordinated_window_minutes: int = field(
        default_factory=lambda: _get_int("COORDINATED_WINDOW_MINUTES", 10)
    )

    coordinated_min_wallets: int = field(
        default_factory=lambda: _get_int("COORDINATED_MIN_WALLETS", 3)
    )

    fresh_wallet_buy_threshold: float = field(
        default_factory=lambda: _get_float("FRESH_WALLET_BUY_THRESHOLD", 50000)
    )

    exchange_unique_sender_threshold: int = field(
        default_factory=lambda: _get_int(
            "EXCHANGE_UNIQUE_SENDER_THRESHOLD", 50
        )
    )

    top_n_wallets_deep_analysis: int = field(
        default_factory=lambda: _get_int(
            "TOP_N_WALLETS_FOR_DEEP_ANALYSIS", 100
        )
    )

    wallet_cache_ttl_days: int = field(
        default_factory=lambda: _get_int("WALLET_CACHE_TTL_DAYS", 7)
    )

    large_flow_alert_threshold: float = field(
        default_factory=lambda: _get_float(
            "LARGE_FLOW_ALERT_THRESHOLD", 100000
        )
    )

    # =========================================================================
    # Smart Money Score Weights
    # =========================================================================

    weight_pnl: float = field(
        default_factory=lambda: _get_float("WEIGHT_PNL", 0.35)
    )

    weight_accumulation: float = field(
        default_factory=lambda: _get_float("WEIGHT_ACCUMULATION", 0.20)
    )

    weight_cross_token: float = field(
        default_factory=lambda: _get_float("WEIGHT_CROSS_TOKEN", 0.15)
    )

    weight_timing: float = field(
        default_factory=lambda: _get_float("WEIGHT_TIMING", 0.15)
    )

    weight_coordinated_fresh: float = field(
        default_factory=lambda: _get_float(
            "WEIGHT_COORDINATED_FRESH", 0.15
        )
    )

    # =========================================================================
    # Known Exchange Wallets
    # =========================================================================

    known_exchange_addresses: list = field(
        default_factory=lambda: [
            a.strip().lower()
            for a in os.getenv(
                "KNOWN_EXCHANGE_ADDRESSES", ""
            ).split(",")
            if a.strip()
        ]
    )

    # =========================================================================
    # Explorer Configuration
    # =========================================================================

    def explorer_base_url(self, chain: str) -> str:
        """
        Returns Explorer API base URL.
        Supports override via EXPLORER_BASE_URL.
        """
        override = os.getenv("EXPLORER_BASE_URL")
        if override:
            return override
        return EXPLORER_V2_BASE_URL

    def chain_id(self, chain: str) -> int:
        """
        Returns Etherscan V2 chain ID.
        """
        if chain not in CHAIN_IDS:
            raise ValueError(
                f"Unsupported chain '{chain}'. Supported: {list(CHAIN_IDS)}"
            )
        return CHAIN_IDS[chain]

    # =========================================================================
    # CoinGecko Configuration
    # =========================================================================

    def coingecko_platform(self, chain: str) -> str:
        override = os.getenv("COINGECKO_PLATFORM")
        if override:
            return override
        return CHAIN_COINGECKO_PLATFORM.get(chain, chain)

    # =========================================================================
    # Validation
    # =========================================================================

    def validate_for_fetch(self):
        if not self.explorer_api_key:
            raise ValueError(
                "EXPLORER_API_KEY is missing. "
                "Set it in your .env file (local) "
                "or GitHub Actions Secrets (CI)."
            )


# ============================================================================
# Global Settings Instance
# ============================================================================

settings = Settings()

# ============================================================================
# Ensure Required Directories Exist
# ============================================================================

for _d in (DATA_RAW_DIR, DATA_CACHE_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
