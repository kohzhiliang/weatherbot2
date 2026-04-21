"""Config loading with validation."""
import json
from pathlib import Path
from dataclasses import dataclass

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

@dataclass
class Config:
    balance: float
    max_bet: float
    min_ev: float
    max_price: float
    max_price_cold: float  # max price for cold-event BUY (≤$0.08 for penny entries)
    max_price_sell: float  # min price for narrow-bucket SELL (≥$0.85 for expensive buckets)
    min_price_buy: float   # minimum entry price for BUY — skip entries below this
    max_bet_conviction: float  # max bet for high-conviction (≥$0.70) entries
    min_volume: float
    min_hours: float
    max_hours: float
    kelly_fraction: float
    max_slippage: float
    scan_interval: int
    monitor_interval: int
    vc_key: str
    polygon_wallet_pk: str
    log_level: str
    disabled_sources: list[str]

    @classmethod
    def load(cls) -> "Config":
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
        return cls(
            balance=float(raw["balance"]),
            max_bet=float(raw["max_bet"]),
            min_ev=float(raw["min_ev"]),
            max_price=float(raw["max_price"]),
            max_price_cold=float(raw.get("max_price_cold", 0.08)),
            max_price_sell=float(raw.get("max_price_sell", 0.85)),
            min_price_buy=float(raw.get("min_price_buy", 0.30)),
            max_bet_conviction=float(raw.get("max_bet_conviction", 50.0)),
            min_volume=float(raw["min_volume"]),
            min_hours=float(raw["min_hours"]),
            max_hours=float(raw["max_hours"]),
            kelly_fraction=float(raw["kelly_fraction"]),
            max_slippage=float(raw["max_slippage"]),
            scan_interval=int(raw["scan_interval_seconds"]),
            monitor_interval=int(raw["monitor_interval_seconds"]),
            vc_key=str(raw.get("vc_key", "")),
            polygon_wallet_pk=str(raw.get("polygon_wallet_pk", "")),
            log_level=str(raw.get("log_level", "INFO")),
            disabled_sources=list(raw.get("disabled_sources", [])),
        )

    def validate(self) -> list[str]:
        errors = []
        if self.balance <= 0:
            errors.append("balance must be > 0")
        if not 0 < self.min_ev <= 1:
            errors.append("min_ev must be between 0 and 1")
        if not 0 < self.kelly_fraction <= 1:
            errors.append("kelly_fraction must be between 0 and 1")
        if self.vc_key == "":
            errors.append("vc_key is required for calibration")
        return errors