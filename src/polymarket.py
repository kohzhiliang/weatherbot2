"""Polymarket API client — market discovery, price fetching, resolution check."""
import re, logging, requests
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)
MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

GAMMA_API = "https://gamma-api.polymarket.com"

def make_slug(city: str, month: str, day: int, year: int) -> str:
    return f"highest-temperature-in-{city}-on-{month}-{day}-{year}"

def parse_temp_range(question: str) -> Optional[tuple[float, float]]:
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))
    m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str: str) -> float:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def _parse_prices(prices):
    """Parse outcomePrices from Polymarket — handles '[0.49, 0.51]' or list."""
    if isinstance(prices, str):
        prices = prices.strip().strip('"').replace('"', '')
        parts = prices.strip("[]").split(",")
        return [float(p.strip()) for p in parts]
    return [float(prices[0]), float(prices[1])]

class PolymarketClient:
    def get_city_markets(self, city_slug: str, month: str, day: int, year: int) -> list[dict]:
        """Get all bucket markets for a city/date."""
        slug = make_slug(city_slug, month, day, year)
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=(5, 8)
            )
            events = r.json()
            if events and isinstance(events, list) and len(events) > 0:
                event = events[0]
                # Markets are embedded directly in the event under "markets"
                embedded = event.get("markets", [])
                if embedded:
                    return self._parse_markets(embedded, slug)
                # Fallback: try marketIds
                market_ids = event.get("marketIds", [])
                if market_ids:
                    markets = []
                    for mid in market_ids:
                        m = self._fetch_market(mid)
                        if m:
                            markets.append(m)
                    return self._parse_markets(markets, slug)
            return []
        except Exception as e:
            log.error("[Polymarket] get_city_markets %s: %s", city_slug, e)
            return []

    def _fetch_market(self, market_id: str) -> Optional[dict]:
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(3, 5))
            return r.json()
        except Exception:
            return None

    def _parse_markets(self, markets: list[dict], slug: str) -> list[dict]:
        result = []
        for m in markets:
            try:
                question = m.get("question", "")
                prices = _parse_prices(m.get("outcomePrices", "[0.5,0.5]"))
                bid = float(m.get("bestBid", prices[0]))
                ask = float(m.get("bestAsk", prices[1]))
                volume = float(m.get("volume", 0))
                result.append({
                    "id": m["id"],
                    "question": question,
                    "range": parse_temp_range(question),
                    "date": m.get("date"),
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "spread": round(ask - bid, 4),
                    "volume": volume,
                    "closed": m.get("closed", False),
                    "end_date": m.get("endDate", ""),
                })
            except Exception as e:
                log.debug("Failed to parse market %s: %s", m.get("id"), e)
        return result

    def check_resolved(self, market_id: str) -> Optional[bool]:
        """Check if market is resolved. True=YES won, False=NO won, None=still open."""
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(5, 8))
            data = r.json()
            closed = data.get("closed", False)
            if not closed:
                return None
            prices = _parse_prices(data.get("outcomePrices", "[0.5,0.5]"))
            yes_price = prices[0]
            if yes_price >= 0.95:
                return True
            elif yes_price <= 0.05:
                return False
            return None
        except Exception as e:
            log.error("[Polymarket] check_resolved %s: %s", market_id, e)
            return None

    def get_current_price(self, market_id: str) -> Optional[float]:
        try:
            r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=(3, 5))
            prices = _parse_prices(r.json().get("outcomePrices", "[0.5,0.5]"))
            return float(prices[0])
        except Exception:
            return None