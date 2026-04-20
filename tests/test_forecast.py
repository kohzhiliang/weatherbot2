import pytest, unittest.mock as mock, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.forecast import LOCATIONS, ForecastEngine

def test_nyc_uses_klga_coords_not_city_center():
    """NYC must use LaGuardia airport coords, not city center."""
    loc = LOCATIONS["nyc"]
    assert loc["station"] == "KLGA"
    assert abs(loc["lat"] - 40.7772) < 0.01
    assert abs(loc["lon"] - (-73.8726)) < 0.01

def test_all_locations_have_station():
    for city, loc in LOCATIONS.items():
        assert "station" in loc
        assert "name" in loc
        assert "unit" in loc
        assert loc["unit"] in ("F", "C")
        assert "region" in loc

def test_ecmwf_returns_dict():
    engine = ForecastEngine("")
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "daily": {
                "time": ["2026-04-20", "2026-04-21"],
                "temperature_2m_max": [75.0, 78.0]
            }
        }
        result = engine.get_ecmwf("nyc", ["2026-04-20", "2026-04-21"])
        assert result == {"2026-04-20": 75, "2026-04-21": 78}

def test_metar_returns_fahrenheit_for_us_city():
    engine = ForecastEngine("")
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = [{"temp": 25.0}]  # Celsius
        result = engine.get_metar("miami")  # Fahrenheit city
        assert result == 77.0  # 25C -> 77F

def test_metar_returns_celsius_for_eu_city():
    engine = ForecastEngine("")
    with mock.patch("requests.get") as mock_get:
        mock_get.return_value.json.return_value = [{"temp": 20.0}]  # Celsius
        result = engine.get_metar("london")  # Celsius city
        assert result == 20.0

def test_engine_picks_hrrr_for_us():
    engine = ForecastEngine("")
    with mock.patch.object(engine, "get_hrrr", return_value={"2026-04-20": 85}):
        with mock.patch.object(engine, "get_ecmwf", return_value={"2026-04-20": 82}):
            snaps = engine.get_forecasts("miami", ["2026-04-20"])
            assert snaps["2026-04-20"]["best_source"] == "hrrr"
            assert snaps["2026-04-20"]["best"] == 85

def test_engine_picks_ecmwf_for_eu():
    engine = ForecastEngine("")
    with mock.patch.object(engine, "get_hrrr", return_value={}):
        with mock.patch.object(engine, "get_ecmwf", return_value={"2026-04-20": 22}):
            snaps = engine.get_forecasts("london", ["2026-04-20"])
            assert snaps["2026-04-20"]["best_source"] == "ecmwf"
            assert snaps["2026-04-20"]["best"] == 22

def test_engine_returns_empty_for_unknown_city():
    engine = ForecastEngine("")
    result = engine.get_forecasts("unknown-city", ["2026-04-20"])
    assert result == {}
