import pytest, tempfile, os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.state import StateDB

@pytest.fixture
def db():
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    name = f.name
    os.unlink(name)
    s = StateDB(name)
    s.init()
    yield s
    os.unlink(name)

def test_open_position_deducts_balance(db):
    db.update_balance(100.0, "init")
    db.open_position({
        "id": "test-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81,
        "entry_price": 0.30, "shares": 10.0, "cost": 3.0,
        "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf",
    })
    assert db.get_balance() == 97.0

def test_close_position_returns_funds_with_profit(db):
    db.update_balance(100.0, "init")
    db.open_position({"id": "test-2", "city": "miami", "date": "2026-04-20",
        "bucket_low": 84, "bucket_high": 85, "entry_price": 0.23,
        "shares": 86.96, "cost": 20.0, "p": 0.8, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 85, "forecast_src": "hrrr"})
    db.close_position("test-2", exit_price=1.0, reason="resolved_win")
    bal = db.get_balance()
    # cost returned (20) + pnl ((1.0-0.23)*86.96 = 66.96) + original 80
    assert abs(bal - 166.96) < 0.1

def test_no_duplicate_positions(db):
    db.update_balance(200.0, "init")
    db.open_position({"id": "dup-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 20.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    result = db.open_position({"id": "dup-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 20.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    assert result == False

def test_get_open_positions(db):
    db.update_balance(100.0, "init")
    db.open_position({"id": "open-1", "city": "nyc", "date": "2026-04-20",
        "bucket_low": 80, "bucket_high": 81, "entry_price": 0.30,
        "shares": 10.0, "cost": 10.0, "p": 0.7, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 81, "forecast_src": "ecmwf"})
    db.open_position({"id": "open-2", "city": "miami", "date": "2026-04-21",
        "bucket_low": 84, "bucket_high": 85, "entry_price": 0.23,
        "shares": 20.0, "cost": 10.0, "p": 0.8, "ev": 0.5, "kelly": 0.25,
        "forecast_temp": 85, "forecast_src": "hrrr"})
    positions = db.get_open_positions()
    assert len(positions) == 2