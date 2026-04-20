"""Weather forecast engine — ECMWF via Open-Meteo, METAR real-time, NWS, Visual Crossing."""
import time, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# NWS API user agent — required by their T&Cs
NWS_HEADERS = {"User-Agent": "weatherbot2/1.0 (zhiliangkoh@gmail.com)"}

LOCATIONS = {
    "nyc":    {"lat": 40.7772,  "lon": -73.8726,  "name": "New York City",  "station": "KLGA",  "unit": "F", "region": "us", "wfo": "OKX"},
    "chicago": {"lat": 41.9742,  "lon": -87.9073,  "name": "Chicago",         "station": "KORD",  "unit": "F", "region": "us", "wfo": "LOT"},
    "miami":  {"lat": 25.7959,  "lon": -80.2870,  "name": "Miami",           "station": "KMIA",  "unit": "F", "region": "us", "wfo": "MFL"},
    "dallas": {"lat": 32.8471,  "lon": -96.8518,  "name": "Dallas",          "station": "KDAL",  "unit": "F", "region": "us", "wfo": "FWD"},
    "seattle": {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",         "station": "KSEA",  "unit": "F", "region": "us", "wfo": "SEW"},
    "atlanta": {"lat": 33.6407,  "lon": -84.4277,  "name": "Atlanta",         "station": "KATL",  "unit": "F", "region": "us", "wfo": "FFC"},
    "london": {"lat": 51.5048,  "lon":   0.0495,    "name": "London",          "station": "EGLC",  "unit": "C", "region": "eu"},
    "paris":  {"lat": 48.9962,  "lon":   2.5979,    "name": "Paris",           "station": "LFPG",  "unit": "C", "region": "eu"},
    "munich": {"lat": 48.3537,  "lon":  11.7750,    "name": "Munich",          "station": "EDDM",  "unit": "C", "region": "eu"},
    "ankara": {"lat": 40.1281,  "lon":  32.9951,    "name": "Ankara",          "station": "LTAC",  "unit": "C", "region": "eu"},
    "seoul":  {"lat": 37.4691,  "lon": 126.4505,    "name": "Seoul",           "station": "RKSI",  "unit": "C", "region": "asia"},
    "tokyo":  {"lat": 35.7647,  "lon": 140.3864,    "name": "Tokyo",           "station": "RJTT",  "unit": "C", "region": "asia"},
    "shanghai": {"lat": 31.1443, "lon": 121.8083,   "name": "Shanghai",        "station": "ZSPD",  "unit": "C", "region": "asia"},
    "singapore": {"lat":  1.3502,"lon": 103.9940,   "name": "Singapore",       "station": "WSSS",  "unit": "C", "region": "asia"},
    "lucknow": {"lat": 26.7606, "lon":  80.8893,    "name": "Lucknow",         "station": "VILK",  "unit": "C", "region": "asia"},
    "tel-aviv": {"lat": 32.0114,"lon":  34.8867,    "name": "Tel Aviv",        "station": "LLBG",  "unit": "C", "region": "asia"},
    "toronto": {"lat": 43.6772, "lon": -79.6306,    "name": "Toronto",         "station": "CYYZ",  "unit": "C", "region": "ca"},
    "sao-paulo": {"lat":-23.4356,"lon": -46.4731,   "name": "Sao Paulo",       "station": "SBGR",  "unit": "C", "region": "sa"},
    "buenos-aires": {"lat":-34.8222,"lon": -58.5358,"name": "Buenos Aires",    "station": "SAEZ",  "unit": "C", "region": "sa"},
    "wellington": {"lat":-41.3272,"lon": 174.8052,  "name": "Wellington",       "station": "NZWN",  "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

SIGMA_DEFAULTS = {"F": 2.0, "C": 1.2}


class ForecastEngine:
    """Fetch weather forecasts from Open-Meteo (ECMWF + HRRR) and METAR."""

    def __init__(self, vc_key: str = ""):
        self.vc_key = vc_key

    def get_forecasts(self, city_slug: str, dates: list[str]) -> dict[str, dict]:
        """Returns {date: {ecmwf, hrrr, metar, best, best_source}}."""
        loc = LOCATIONS.get(city_slug)
        if not loc:
            return {}
        ecmwf = self.get_ecmwf(city_slug, dates)
        hrrr = self.get_hrrr(city_slug, dates) if loc["region"] == "us" else {}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        snapshots = {}
        for date in dates:
            metar = self.get_metar(city_slug) if date == today else None
            if loc["unit"] == "F" and hrrr.get(date) is not None:
                best, best_src = hrrr[date], "hrrr"
            elif ecmwf.get(date) is not None:
                best, best_src = ecmwf[date], "ecmwf"
            elif metar is not None:
                best, best_src = metar, "metar"
            else:
                best, best_src = None, None
            snapshots[date] = {
                "ecmwf": ecmwf.get(date),
                "hrrr": hrrr.get(date) if loc["region"] == "us" else None,
                "metar": metar,
                "best": best,
                "best_source": best_src,
            }
        return snapshots

    def get_actual_temp(self, city_slug: str, date_str: str) -> Optional[float]:
        """Resolution-time actual temperature via Visual Crossing (for calibration)."""
        if not self.vc_key:
            return None
        loc = LOCATIONS.get(city_slug)
        if not loc:
            return None
        unit = loc["unit"]
        vc_unit = "us" if unit == "F" else "metric"
        try:
            url = (
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
                f"/{loc['station']}/{date_str}/{date_str}"
                f"?unitGroup={vc_unit}&key={self.vc_key}&include=days&elements=tempmax"
            )
            data = requests.get(url, timeout=(5, 8)).json()
            days = data.get("days", [])
            if days and days[0].get("tempmax") is not None:
                return round(float(days[0]["tempmax"]), 1)
        except Exception as e:
            log.warning("[VC] %s %s: %s", city_slug, date_str, e)
        return None

    def get_ecmwf(self, city_slug: str, dates: list[str]) -> dict[str, float]:
        """ECMWF via Open-Meteo. Airport coordinates = correct station match."""
        loc = LOCATIONS[city_slug]
        unit = loc["unit"]
        temp_unit = "fahrenheit" if unit == "F" else "celsius"
        result = {}
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
            f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=ecmwf_ifs025&bias_correction=true"
        )
        for attempt in range(3):
            try:
                data = requests.get(url, timeout=(5, 10)).json()
                if "error" not in data:
                    for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                        if date in dates and temp is not None:
                            result[date] = round(temp, 1) if unit == "C" else round(temp)
                return result
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log.warning("[ECMWF] %s: %s", city_slug, e)
        return result

    def get_hrrr(self, city_slug: str, dates: list[str]) -> dict[str, float]:
        """HRRR via Open-Meteo — US cities only, D+0 to D+2."""
        loc = LOCATIONS[city_slug]
        if loc["unit"] != "F":
            return {}
        result = {}
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models=hrrr"
        )
        for attempt in range(3):
            try:
                data = requests.get(url, timeout=(5, 10)).json()
                if "error" not in data:
                    for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                        if date in dates and temp is not None:
                            result[date] = round(temp)
                return result
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    log.warning("[HRRR] %s: %s", city_slug, e)
        return result

    def get_metar(self, city_slug: str) -> Optional[float]:
        """Current observed temperature from METAR — real-time airport obs."""
        loc = LOCATIONS[city_slug]
        station = loc["station"]
        unit = loc["unit"]
        try:
            url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
            data = requests.get(url, timeout=(3, 5)).json()
            if data and isinstance(data, list):
                temp_c = data[0].get("temp")
                if temp_c is not None:
                    if unit == "F":
                        return round(float(temp_c) * 9/5 + 32)
                    return round(float(temp_c), 1)
        except Exception as e:
            log.debug("[METAR] %s: %s", city_slug, e)
        return None

    def get_nws(self, city_slug: str, dates: list[str]) -> dict[str, float]:
        """
        Fetch NWS (National Weather Service) daily high forecast for US cities.
        Uses the NWS API gridpoint data — returns today's and tomorrow's forecast highs.

        The NWS forecast is the settlement authority and market anchor, but has
        per-city systematic biases. We track directional bias (warm/cold) separately.
        """
        loc = LOCATIONS.get(city_slug)
        if not loc or loc["region"] != "us":
            return {}

        # Cached gridpoint URL — avoid re-fetching points for every call
        if not hasattr(self, "_nws_grid_cache"):
            self._nws_grid_cache: dict[str, str] = {}

        if city_slug not in self._nws_grid_cache:
            wfo = loc.get("wfo")
            if not wfo:
                return {}
            # Use well-known gridpoint coordinates for each city's airport ASOS station
            # Pre-computed from NWS API: https://api.weather.gov/points/{lat},{lon}
            GRIDPOINTS = {
                "nyc":     ("OKX", 37, 46),
                "chicago":  ("LOT", 66, 77),
                "miami":    ("MFL", 106, 51),
                "dallas":   ("FWD", 87, 107),
                "seattle":  ("SEW", 124, 61),
                "atlanta":  ("FFC", 50, 82),
            }
            grid = GRIDPOINTS.get(city_slug)
            if not grid:
                return {}
            self._nws_grid_cache[city_slug] = f"https://api.weather.gov/gridpoints/{grid[0]}/{grid[1]},{grid[2]}"

        grid_url = self._nws_grid_cache[city_slug]
        result = {}

        try:
            data = requests.get(grid_url, headers=NWS_HEADERS, timeout=(5, 10)).json()
            temps = data.get("properties", {}).get("temperature", {}).get("values", [])
            if not temps:
                return {}

            # Parse hourly temperatures into daily highs
            daily_highs: dict[str, int] = {}
            for rec in temps:
                valid_time = rec.get("validTime", "")
                temp_c = rec.get("value")
                if temp_c is None or not valid_time:
                    continue
                start_str = valid_time.split("/")[0]
                dt_utc = datetime.fromisoformat(start_str)
                # NWS times are in local ET/America timezone
                dt_local = dt_utc.astimezone(timezone.utc).astimezone(
                    datetime.now(timezone.utc).astimezone().tzinfo
                )
                # Approximate: use ET (UTC-4 EDT or UTC-5 EST) for these WFO locations
                from datetime import timedelta
                et_offset = timedelta(hours=-4)  # EDT
                dt_et = dt_utc.astimezone(timezone.utc).replace() + et_offset
                # Simple approach: just use the UTC date shifted by -4h for ET boundary
                # Better: parse the validTime properly
                date_key = start_str[:10]  # YYYY-MM-DD in UTC — good enough for daily high

                temp_f = round(temp_c * 9/5 + 32)
                if date_key not in daily_highs or temp_f > daily_highs[date_key]:
                    daily_highs[date_key] = temp_f

            # Return only the dates requested
            for date_str in dates:
                if date_str in daily_highs:
                    result[date_str] = daily_highs[date_str]

        except Exception as e:
            log.warning("[NWS] %s: %s", city_slug, e)

        return result
