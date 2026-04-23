"""Weatherbot V2 — main entry point."""
import sys, os, logging, time, signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
from src.state import StateDB
from src.forecast import ForecastEngine
from src.polymarket import PolymarketClient
from src.scanner import Scanner
from src.whale_monitor import WhaleWatcher

WHALE_SCAN_INTERVAL = 120  # seconds — whale scan runs every 2 min

BOT_DIR = Path(__file__).parent.parent
LOG_DIR = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "weatherbot.db")


def main():
    cfg = Config.load()
    errs = cfg.validate()
    if errs:
        for e in errs:
            if "vc_key" in e:
                log.warning("Config warning: %s — using default sigma", e)
            else:
                log.error("Config error: %s", e)

    if cfg.polygon_wallet_pk:
        log.info("Wallet configured — LIVE MODE")
    else:
        log.info("No wallet — PAPER MODE")

    state = StateDB(DB_PATH)
    state.init()

    if state.get_balance() == 0.0:
        state.update_balance(cfg.balance, "init")

    fc = ForecastEngine(cfg.vc_key, cfg.disabled_sources,
                        bias_provider=state.get_ecmwf_bias)
    pm = PolymarketClient()
    scanner = Scanner(cfg, state, fc, pm)
    whale_watcher = WhaleWatcher(DB_PATH, cfg)

    log.info("Weatherbot V2 starting | balance=$%.2f | max_bet=$%.2f | scan_interval=%ds",
             cfg.balance, cfg.max_bet, cfg.scan_interval)

    def shutdown(signum, frame):
        log.info("Shutting down — saving state...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_full_scan = 0
    last_whale_scan = 0
    while True:
        now = time.time()
        if now - last_full_scan >= cfg.scan_interval:
            log.info("=== FULL SCAN ===")
            try:
                new_pos, closed, resolved = scanner.run_full_scan()
                bal = state.get_balance()
                open_count = len(state.get_open_positions())
                log.info("Scan done | balance=$%.2f | open=%d | new=%d | closed=%d | resolved=%d",
                         bal, open_count, new_pos, closed, resolved)
                last_full_scan = time.time()
            except Exception as e:
                log.error("Scan error: %s", e, exc_info=True)
                time.sleep(30)
                continue

        # Whale scan every 2 minutes
        if now - last_whale_scan >= WHALE_SCAN_INTERVAL:
            try:
                c, r = whale_watcher.scan()
                if c or r:
                    log.info("Whale scan | cloned=%d resolved=%d", c, r)
                last_whale_scan = time.time()
            except Exception as e:
                log.error("Whale scan error: %s", e)

        time.sleep(cfg.monitor_interval)


if __name__ == "__main__":
    main()
