import pytest, unittest.mock as mock, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.polymarket import parse_temp_range, make_slug, PolymarketClient, _parse_prices

def test_parse_between_bucket():
    q = "Will the highest temperature in Miami be between 84-85°F on April 20?"
    assert parse_temp_range(q) == (84.0, 85.0)

def test_parse_or_below():
    q = "Will the highest temperature in NYC be 73°F or below on April 20?"
    assert parse_temp_range(q) == (-999.0, 73.0)

def test_parse_or_above():
    q = "Will the highest temperature in Tokyo be 35°C or higher on July 15?"
    assert parse_temp_range(q) == (35.0, 999.0)

def test_parse_exact():
    q = "Will the highest temperature in London be 25°C on June 1?"
    assert parse_temp_range(q) == (25.0, 25.0)

def test_parse_returns_none_for_weird_input():
    assert parse_temp_range(None) is None
    assert parse_temp_range("") is None
    assert parse_temp_range("Will this happen?") is None

def test_market_slug_construction():
    assert make_slug("nyc", "april", 20, 2026) == "highest-temperature-in-nyc-on-april-20-2026"

def test_parse_prices_string():
    assert _parse_prices("[0.49,0.51]") == [0.49, 0.51]
    assert _parse_prices("\"[0.49,0.51]\"") == [0.49, 0.51]

def test_parse_prices_list():
    assert _parse_prices([0.49, 0.51]) == [0.49, 0.51]

def test_check_resolved_win():
    client = PolymarketClient()
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "closed": True, "outcomePrices": "[0.97,0.03]"
        }
        result = client.check_resolved("mid-1")
        assert result == True

def test_check_resolved_loss():
    client = PolymarketClient()
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "closed": True, "outcomePrices": "[0.04,0.96]"
        }
        result = client.check_resolved("mid-1")
        assert result == False

def test_check_resolved_still_open():
    client = PolymarketClient()
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "closed": False, "outcomePrices": "[0.49,0.51]"
        }
        result = client.check_resolved("mid-1")
        assert result == None

def test_get_city_markets_returns_list():
    client = PolymarketClient()
    mock_event = {"marketIds": ["m1", "m2"]}
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.side_effect = [
            [mock_event],  # events response
            {"id": "m1", "question": "Will highest temp in NYC be between 80-81°F?", 
             "outcomePrices": "[0.49,0.51]", "bestBid": 0.49, "bestAsk": 0.51, "volume": 5000.0,
             "closed": False, "endDate": "2026-04-20T12:00:00Z"},
            {"id": "m2", "question": "Will highest temp in NYC be between 81-82°F?",
             "outcomePrices": "[0.48,0.52]", "bestBid": 0.48, "bestAsk": 0.52, "volume": 4000.0,
             "closed": False, "endDate": "2026-04-20T12:00:00Z"},
        ]
        markets = client.get_city_markets("nyc", "april", 20, 2026)
        assert len(markets) == 2
        assert markets[0]["bid"] == 0.49
        assert markets[0]["ask"] == 0.51
        assert markets[0]["range"] == (80.0, 81.0)