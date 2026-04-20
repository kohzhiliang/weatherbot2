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

    @classmethod
    def load(cls) -> "Config":
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
        return cls(
            balance=float(raw["balance"]),
            max_bet=float(raw["max_bet"]),
            min_ev=float(raw["min_ev"]),
            max_price=float(raw["max_price"]),
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