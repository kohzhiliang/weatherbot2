"""Main scanning engine — orchestrates forecast + market data into trading decisions."""
import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Config
from .forecast import ForecastEngine, LOCATIONS, COLD_CITY_PRIORITY
from .polymarket import PolymarketClient, hours_to_resolution
from .betsizing import bucket_prob, calc_ev, calc_kelly, bet_size, calc_kelly_penny
from .state import StateDB

log = logging.getLogger(__name__)
MONTHS = [
    "january","february","march","april","may","june",
    "july","august","september","october","november","december"
]

SCAN_TIMEOUT = 40  # seconds — total budget for full scan
MAX_WORKERS = 8     # parallel threads for city processing
METAR_CACHE_TTL = 300  # 5 minutes — METAR doesn't change fast

# In-memory METAR cache keyed by city_slug
_metar_cache: dict[str, tuple[float, float]] = {}  # {city_slug: (temp, cached_at)}


class Scanner:
    def __init__(self, cfg: Config, state: StateDB, fc: ForecastEngine, pm: PolymarketClient):
        self.cfg = cfg
        self.state = state
        self.fc = fc
        self.pm = pm

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def run_full_scan(self) -> tuple[int, int, int]:
        """
        Full scan: check all cities, all dates — parallelized.
        Returns (new_positions, closed, resolved).
        """
        scan_start = time.time()
        now = datetime.now(timezone.utc)
        dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]

        open_pos_ids = {p["id"] for p in self.state.get_open_positions()}

        # Phase 1: Pre-check Polymarket markets — which cities have active markets?
        # This runs first so we skip forecast fetches for cities with nothing to trade
        cities_with_markets = self._precheck_markets(dates)
        log.info("[SCAN] Cities with active markets: %d/%d", len(cities_with_markets), len(LOCATIONS))

        # Phase 2: Parallel forecast fetch for cities that have markets
        city_forecasts = self._fetch_forecasts_parallel(cities_with_markets, dates, scan_start)

        # Phase 3: Process each city — evaluate forecasts against markets
        new_pos, closed, resolved = 0, 0, 0

        for city_slug, snaps in city_forecasts.items():
            if time.time() - scan_start > SCAN_TIMEOUT:
                log.warning("[SCAN] Timeout at %s — stopping", city_slug)
                break

            loc = LOCATIONS[city_slug]
            pos_result = self._process_city(city_slug, loc, dates, snaps, open_pos_ids)
            if pos_result:
                new_c, closed_c = pos_result
                new_pos += new_c
                closed += closed_c

        # Check for resolved markets (single-threaded, fast)
        for pos in self.state.get_open_positions():
            result = self.pm.check_resolved(pos["id"])
            if result is not None:
                exit_price = 1.0 if result else 0.0
                self.state.close_position(pos["id"], exit_price, f"resolved_{'win' if result else 'loss'}")
                resolved += 1
                log.info("[RESOLVED] %s %s -> %s", pos["city"], pos["date"], "WIN" if result else "LOSS")
                # Record NWS bias for this resolution
                actual_temp = self.fc.get_actual_temp(pos["city"], pos["date"])
                if actual_temp is not None:
                    nws_fc = pos.get("nws_forecast")
                    model_fc = pos.get("forecast_temp")
                    self.state.record_nws_bias(pos["city"], actual_temp, nws_fc, model_fc)
                    log.info("[BIAS] %s %s actual=%s nws=%s model=%s",
                             pos["city"], pos["date"],
                             f"{actual_temp}°F" if actual_temp else "N/A",
                             f"{nws_fc}°F" if nws_fc else "N/A",
                             f"{model_fc}°F" if model_fc else "N/A")

        elapsed = time.time() - scan_start
        log.info("[SCAN] done in %.1fs | new=%d closed=%d resolved=%d", elapsed, new_pos, closed, resolved)
        return new_pos, closed, resolved

    # -------------------------------------------------------------------------
    # Phase 1: Market pre-check
    # -------------------------------------------------------------------------

    def _precheck_markets(self, dates: list[str]) -> list[str]:
        """
        Fast check: which cities have ANY Polymarket markets in the next 4 days?
        Skips cities with no active markets before wasting forecast API calls.
        Uses batched Gamma API calls with slug wildcards to be efficient.
        """
        cities_with_markets = []

        for city_slug, loc in LOCATIONS.items():
            for date_str in dates:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                month_name = MONTHS[dt.month - 1]
                markets = self._safe_get_markets(city_slug, month_name, dt.day, dt.year)
                if markets:
                    cities_with_markets.append(city_slug)
                    break  # Found at least one — city is worth scanning
                time.sleep(0.05)  # Gentle rate limit

        return cities_with_markets

    def _safe_get_markets(self, city_slug: str, month: str, day: int, year: int) -> list[dict]:
        """Wrapper with error handling and timeout."""
        try:
            return self.pm.get_city_markets(city_slug, month, day, year)
        except Exception as e:
            log.debug("[Scan] market precheck error %s: %s", city_slug, e)
            return []

    # -------------------------------------------------------------------------
    # Phase 2: Parallel forecast fetch
    # -------------------------------------------------------------------------

    def _fetch_forecasts_parallel(self, city_slugs: list[str], dates: list[str],
                                   scan_start: float) -> dict[str, dict]:
        """
        Fetch forecasts for all cities in parallel using ThreadPoolExecutor.
        Returns {city_slug: {date: {ecmwf, hrrr, metar, best, best_source}}}.
        """
        results: dict[str, dict] = {}
        remaining_budget = SCAN_TIMEOUT - (time.time() - scan_start)

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(city_slugs))) as ex:
            futures = {
                ex.submit(self._fetch_city_forecast, city_slug, dates): city_slug
                for city_slug in city_slugs
            }

            for future in as_completed(futures):
                city_slug = futures[future]
                elapsed = time.time() - scan_start

                if elapsed >= remaining_budget:
                    log.warning("[SCAN] Timeout during forecast fetch, cancelling remaining")
                    results[city_slug] = {}
                    continue

                try:
                    snaps = future.result()
                    if snaps:
                        results[city_slug] = snaps
                except Exception as e:
                    log.error("[Scan] forecast fetch error %s: %s", city_slug, e)
                    results[city_slug] = {}

        return results

    def _fetch_city_forecast(self, city_slug: str, dates: list[str]) -> dict:
        """Fetch all forecasts for one city — used by parallel executor."""
        loc = LOCATIONS[city_slug]
        snapshots = {}

        ecmwf = self.fc.get_ecmwf(city_slug, dates)
        hrrr = {}
        nws = {}
        if loc["region"] == "us":
            hrrr = self.fc.get_hrrr(city_slug, dates)
            nws = self.fc.get_nws(city_slug, dates)

        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        for date_str in dates:
            metar = None
            if date_str == today:
                metar = self._get_cached_metar(city_slug)

            # Source priority for best forecast: HRRR > ECMWF > METAR
            # NWS is kept separate — used as settlement anchor and market reference
            if loc["unit"] == "F" and hrrr.get(date_str) is not None:
                best, best_src = hrrr[date_str], "hrrr"
            elif ecmwf.get(date_str) is not None:
                best, best_src = ecmwf[date_str], "ecmwf"
            elif metar is not None:
                best, best_src = metar, "metar"
            else:
                best, best_src = None, None

            snapshots[date_str] = {
                "ecmwf": ecmwf.get(date_str),
                "hrrr": hrrr.get(date_str) if loc["region"] == "us" else None,
                "metar": metar,
                "nws": nws.get(date_str) if loc["region"] == "us" else None,
                "best": best,
                "best_source": best_src,
            }

        return snapshots

    def _get_cached_metar(self, city_slug: str) -> Optional[float]:
        """Return cached METAR temp if fresh (< 5 min old), else fetch fresh."""
        now = time.time()
        if city_slug in _metar_cache:
            temp, cached_at = _metar_cache[city_slug]
            if now - cached_at < METAR_CACHE_TTL:
                return temp

        # Fetch fresh
        metar = self.fc.get_metar(city_slug)
        if metar is not None:
            _metar_cache[city_slug] = (metar, now)
        return metar

    # -------------------------------------------------------------------------
    # Phase 3: Evaluate forecasts against markets
    # -------------------------------------------------------------------------

    def _process_city(self, city_slug: str, loc: dict, dates: list[str],
                     snaps: dict, open_pos_ids: set[str]) -> Optional[tuple[int, int]]:
        """Process one city's forecasts and find trade signals."""
        new_pos, closed = 0, 0

        for date_str in dates:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month_name = MONTHS[dt.month - 1]
            markets = self._safe_get_markets(city_slug, month_name, dt.day, dt.year)
            if not markets:
                continue
            time.sleep(0.1)  # Polymarket rate limit

            snap = snaps.get(date_str, {})
            forecast_temp = snap.get("best")
            best_src = snap.get("best_source")
            if forecast_temp is None:
                continue

            hours_left = hours_to_resolution(markets[0]["end_date"]) if markets else 0

            if markets[0]["id"] in open_pos_ids:
                # Monitor existing position
                c = self._check_close(markets[0]["id"], forecast_temp, markets, loc, snap)
                if c:
                    closed += c
            else:
                # Open new position
                if hours_left < self.cfg.min_hours or hours_left > self.cfg.max_hours:
                    continue
                sig = self._find_signal(city_slug, forecast_temp, markets, best_src, loc, hours_left, snap)
                if sig:
                    ok = self.state.open_position(sig)
                    if ok:
                        new_pos += 1
                        log.info(
                            "[OPEN] %s %s %s -> %s | p=%.2f ev=%.2f cost=$%.2f",
                            loc["name"], date_str,
                            f"{forecast_temp}°{loc['unit']}",
                            f"{sig['bucket_low']}-{sig['bucket_high']}",
                            sig["p"], sig["ev"], sig["cost"]
                        )

        return (new_pos, closed) if (new_pos or closed) else None

    def _find_signal(self, city_slug: str, forecast_temp: float, markets: list[dict],
                     best_src: str, loc: dict, hours_left: float, snap: dict) -> Optional[dict]:
        """
        ColdMath-inspired multi-signal strategy: three distinct signal types.

        1. COLD-EVENT BUY (primary): Buy bounded "X°C or below" buckets at $0.01-$0.08.
           ColdMath's edge: market underestimates cold weather reliability.
           p must be > 0.05 for Kelly to apply. Below that: use hybrid penny Kelly.
           Skip unbounded buckets entirely (Kelly meaningless).

        2. NARROW-BUCKET SELL (secondary): Sell expensive single-degree buckets
           at $0.85+ where our forecast is confidently OUTSIDE the bucket.
           ColdMath's biggest wins came from selling narrow buckets others over-bought.
           Max loss capped at 2× premium collected.

        3. CONVICTION BUY (tertiary): Buy bounded buckets with p > 0.05 at any price
           up to max_price using standard Kelly.

        Bucket rules:
        - Single-degree buckets: NEVER BUY, only SELL if price ≥ $0.85
        - Unbounded buckets (t_low=-999 or t_high=999): NEVER BUY
        - "Or below" / "or higher" range buckets: OK to BUY at cheap prices
        - All BUY entries must have ask ≥ min_price_buy ($0.30)

        NWS deviation signal: if HRRR/ECMWF deviates from NWS by 2°F+,
        the market may be anchored to NWS and mispriced. We boost EV in that case.
        """
        unit = loc["unit"]
        sigma = self.state.get_calibration(city_slug, best_src)
        sigma = sigma or (2.0 if unit == "F" else 1.5)

        nws_temp = snap.get("nws")
        nws_deviation = 0
        if nws_temp is not None and forecast_temp is not None:
            nws_deviation = forecast_temp - nws_temp  # positive = model warmer than NWS

        candidates = []

        for m in markets:
            rng = m.get("range")
            if not rng:
                continue
            t_low, t_high = rng
            bid, ask = m["bid"], m["ask"]
            spread = m.get("spread", 0)
            volume = m.get("volume", 0)
            question = m.get("question", "")

            if volume < self.cfg.min_volume:
                continue
            if spread > self.cfg.max_slippage:
                continue

            bucket_width = abs(t_high - t_low)
            is_single_degree = bucket_width <= 1.0
            is_or_below = bool(re.search(r"or below|below \d+", question, re.IGNORECASE))
            is_unbounded = (t_low == -999.0 or t_high == 999.0)

            # Classify market type for COLD_SELL signal
            # LOW_MIN: forecast below bucket_low (bucket is "X or above" priced high — overbought by optimists)
            # HIGH_MAX: forecast above bucket_high (bucket is "X or below" priced high — overbought by cold-huggers)
            # RANGE: forecast inside bucket
            market_type = "RANGE"
            if forecast_temp is not None:
                if forecast_temp < t_low:
                    market_type = "LOW_MIN"
                elif forecast_temp > t_high:
                    market_type = "HIGH_MAX"

            # ---------------------------------------------------------------
            # SIGNAL TYPE 1: COLD-EVENT BUY
            # Buy bounded "X or below" buckets at cheap prices (≤ max_price_cold).
            # Skip unbounded buckets — ColdMath never buys these.
            # Skip single-degree buckets at <$0.70 — only trade narrow buckets as SELLs.
            # ---------------------------------------------------------------
            if (ask <= self.cfg.max_price_cold
                    and not is_single_degree
                    and not is_unbounded
                    and (is_or_below or self._is_cold_city_in_range(city_slug, forecast_temp, t_low, t_high))):

                p = bucket_prob(forecast_temp, t_low, t_high, sigma)

                # P2: Kelly only applies to bounded buckets with p > 0.05.
                # Below p=0.01: skip regardless (lottery ticket).
                # Below p=0.05 but above p=0.01: use hybrid penny Kelly.
                if p < 0.01:
                    continue
                if p < 0.05:
                    kelly = calc_kelly_penny(p, ask, self.cfg.kelly_fraction)
                else:
                    kelly = calc_kelly(p, ask) * self.cfg.kelly_fraction

                ev = calc_ev(p, ask)
                if ask < 0.05:
                    continue

                balance = self.state.get_balance()
                # P1: COLD_BUY uses max_bet_conviction ($50) for cold-city conviction trades
                size = bet_size(kelly, balance, self.cfg.max_bet_conviction)
                if size < 0.50:
                    continue

                shares = round(size / ask, 2) if ask > 0 else 0
                candidates.append({
                    "type": "COLD_BUY",
                    "id": m["id"],
                    "city": city_slug,
                    "date": m.get("date"),
                    "bucket_low": t_low,
                    "bucket_high": t_high,
                    "entry_price": ask,
                    "bid_at_entry": bid,
                    "spread": spread,
                    "shares": shares,
                    "cost": round(shares * ask, 2),
                    "p": round(p, 3),
                    "ev": round(ev, 3),
                    "kelly": round(kelly, 3),
                    "forecast_temp": forecast_temp,
                    "forecast_src": best_src,
                    "nws_forecast": nws_temp,
                    "side": "BUY",
                })
                continue

            # ---------------------------------------------------------------
            # SIGNAL TYPE 2: NARROW-BUCKET SELL
            # Sell single-degree buckets priced ≥ $0.85 where we're confident
            # the temp is OUTSIDE this bucket.
            # ---------------------------------------------------------------
            if (is_single_degree
                    and ask >= self.cfg.max_price_sell
                    and not (t_low <= float(forecast_temp) <= t_high)):

                p_inside = bucket_prob(forecast_temp, t_low, t_high, sigma)
                ev = calc_ev(1.0 - p_inside, 1.0 - ask)

                if ev < self.cfg.min_ev:
                    continue
                if ask < 0.10:
                    continue  # Garbage tick

                # P2: Max loss = 2× premium collected.
                # premium = shares × ask,  max_loss = shares × (1 - ask)
                # Setting max_loss = 2 × premium:
                #   shares × (1 - ask) = 2 × shares × ask
                #   shares × (1 - ask - 2×ask) = 0
                #   1 - 3×ask = 0  →  ask = 1/3 ≈ $0.333
                # At ask ≥ $0.85: max_loss = shares × (1 - 0.85) = 0.15 × shares
                #                 premium = shares × 0.85
                #                 max_loss / premium = 0.15 / 0.85 ≈ 17.6%  < 2× ✓
                shares = min(
                    round(self.cfg.max_bet / max(1.0 - ask, 0.01), 2),
                    150,
                )
                premium_collected = round(shares * ask, 2)
                max_loss = round(shares * (1.0 - ask), 2)

                # P2: enforce max_loss ≤ 2× premium
                if max_loss > 2 * premium_collected:
                    # Reduce shares to satisfy constraint
                    shares = round(2 * premium_collected / max(1.0 - ask, 0.01), 2)
                    shares = max(shares, 1)
                    max_loss = round(shares * (1.0 - ask), 2)
                    premium_collected = round(shares * ask, 2)

                if premium_collected < 0.50:
                    continue  # Not worth the gas

                candidates.append({
                    "type": "NARROW_SELL",
                    "id": m["id"],
                    "city": city_slug,
                    "date": m.get("date"),
                    "bucket_low": t_low,
                    "bucket_high": t_high,
                    "entry_price": ask,
                    "bid_at_entry": bid,
                    "spread": spread,
                    "shares": shares,
                    "cost": round(-shares * ask, 2),
                    "p": round(p_inside, 3),
                    "ev": round(ev, 3),
                    "kelly": 0.0,
                    "forecast_temp": forecast_temp,
                    "forecast_src": best_src,
                    "nws_forecast": nws_temp,
                    "side": "SELL",
                })
                continue

            # ---------------------------------------------------------------
            # SIGNAL TYPE 3: COLD SELL (HIGH_MAX — forecast above bucket)
            # When forecast exceeds the bucket high, the market is overestimating
            # the chance of the lower bucket. Sell the HIGH_MAX bucket.
            # e.g. bucket is "31°C or below" trading at $0.75, but ECMWF says 33°C.
            # Only for cold cities where we have conviction, and only at ≥$0.70.
            # P1: max_bet_conviction=$50 for cold-city conviction trades.
            # ---------------------------------------------------------------
            if (market_type == "HIGH_MAX"
                    and not is_unbounded
                    and self._is_cold_city_in_range(city_slug, forecast_temp, t_low, t_high)
                    and ask >= 0.70):

                p_above = 1.0 - bucket_prob(forecast_temp, t_low, t_high, sigma)
                ev = calc_ev(p_above, 1.0 - ask)

                if ev < self.cfg.min_ev:
                    continue

                kelly = calc_kelly(p_above, 1.0 - ask) * self.cfg.kelly_fraction
                balance = self.state.get_balance()
                # P1: use conviction cap for cold-city sells
                size = bet_size(kelly, balance, self.cfg.max_bet_conviction)
                if size < 0.50:
                    continue

                shares = round(size / ask, 2) if ask > 0 else 0
                premium_collected = round(shares * ask, 2)
                max_loss = round(shares * (1.0 - ask), 2)

                # P2: enforce max_loss ≤ 2× premium
                if max_loss > 2 * premium_collected:
                    shares = round(2 * premium_collected / max(1.0 - ask, 0.01), 2)
                    shares = max(shares, 1)
                    max_loss = round(shares * (1.0 - ask), 2)
                    premium_collected = round(shares * ask, 2)

                if premium_collected < 0.50:
                    continue  # Not worth the gas

                candidates.append({
                    "type": "COLD_SELL",
                    "id": m["id"],
                    "city": city_slug,
                    "date": m.get("date"),
                    "bucket_low": t_low,
                    "bucket_high": t_high,
                    "entry_price": ask,
                    "bid_at_entry": bid,
                    "spread": spread,
                    "shares": shares,
                    "cost": round(-shares * ask, 2),
                    "p": round(p_above, 3),
                    "ev": round(ev, 3),
                    "kelly": round(kelly, 3),
                    "forecast_temp": forecast_temp,
                    "forecast_src": best_src,
                    "nws_forecast": nws_temp,
                    "side": "SELL",
                })
                continue

            # ---------------------------------------------------------------
            # SIGNAL TYPE 4: CONVICTION BUY (standard bounded buckets, p > 0.05)
            # Standard Kelly on bounded buckets where we're confident.
            # P1: max_bet_conviction=$50 for high-conviction entries.
            # ---------------------------------------------------------------
            if (not is_single_degree
                    and not is_unbounded
                    and ask >= self.cfg.min_price_buy
                    and (t_low <= float(forecast_temp) <= t_high)):

                p = bucket_prob(forecast_temp, t_low, t_high, sigma)
                if p < 0.05:
                    continue  # p too low for standard Kelly

                ev = calc_ev(p, ask)
                if ev < self.cfg.min_ev:
                    continue

                kelly = calc_kelly(p, ask) * self.cfg.kelly_fraction
                balance = self.state.get_balance()
                # P1: CONVICTION_BUY uses $50 cap (max_bet_conviction)
                size = bet_size(kelly, balance, self.cfg.max_bet_conviction)
                if size < 0.50:
                    continue

                shares = round(size / ask, 2) if ask > 0 else 0
                candidates.append({
                    "type": "CONVICTION_BUY",
                    "id": m["id"],
                    "city": city_slug,
                    "date": m.get("date"),
                    "bucket_low": t_low,
                    "bucket_high": t_high,
                    "entry_price": ask,
                    "bid_at_entry": bid,
                    "spread": spread,
                    "shares": shares,
                    "cost": round(shares * ask, 2),
                    "p": round(p, 3),
                    "ev": round(ev, 3),
                    "kelly": round(kelly, 3),
                    "forecast_temp": forecast_temp,
                    "forecast_src": best_src,
                    "nws_forecast": nws_temp,
                    "side": "BUY",
                })

        if not candidates:
            return None

        # NWS deviation boost
        for c in candidates:
            if loc["region"] == "us" and abs(nws_deviation) >= 2:
                ev_boost = min(0.10, abs(nws_deviation) * 0.015)
                c["ev"] = round(c["ev"] + ev_boost, 3)
                log.debug(
                    "[NWS] %s deviation=%+d°F | boost=%.3f | EV: %.3f",
                    city_slug, nws_deviation, ev_boost, c["ev"]
                )

        # Filter by min_ev and pick best
        viable = [c for c in candidates if c["ev"] >= self.cfg.min_ev]
        if not viable:
            return None

        best = max(viable, key=lambda c: c["ev"])
        log.debug(
            "[SIGNAL] %s %s %s | type=%s p=%.2f ev=%.2f cost=$%.2f",
            city_slug, best["date"],
            f"{forecast_temp}°{unit}",
            best["type"], best["p"], best["ev"], abs(best.get("cost", 0))
        )
        return best
    def _is_cold_city_in_range(self, city_slug: str, fc_temp: float,
                                t_low: float, t_high: float) -> bool:
        """
        Is this a ColdMath-priority city with forecast in/near a cold bucket?
        ColdMath bought pennies on spring cities that reliably get cold.
        We check if the bucket is plausibly cold for that city in April.
        """
        cold_info = COLD_CITY_PRIORITY.get(city_slug)
        if not cold_info:
            return False

        unit = LOCATIONS[city_slug].get("unit", "C")

        # For "or below" buckets: check if threshold is at or below typical low
        if unit == "C":
            typical_low = cold_info.get("typical_low_c", 10)
            typical_high = cold_info.get("typical_high_c", 20)
        else:
            typical_low = cold_info.get("typical_low_f", 40)
            typical_high = cold_info.get("typical_high_f", 70)

        # Bucket midpoint (handle unbounded)
        if t_high == 999.0:  # "X or higher" — not a cold bucket
            return False
        if t_low == -999.0:  # "X or below"
            # Cold bucket if threshold is near/at typical_low or below
            return t_high <= typical_high
        else:
            # Range bucket — cold if entire range is at/below typical
            return t_high <= typical_high

    def _check_close(self, pos_id: str, forecast_temp: float, markets: list[dict],
                     loc: dict, snap: dict) -> int:
        """Check if position should be closed for stop-loss, trailing stop, or forecast shift."""
        open_pos = {p["id"]: p for p in self.state.get_open_positions()}
        if pos_id not in open_pos:
            return 0
        pos = open_pos[pos_id]

        current_price = None
        for m in markets:
            if m["id"] == pos_id:
                current_price = m.get("bid")
                break
        if current_price is None:
            return 0

        entry = pos["entry_price"]
        stop = pos.get("stop_price", entry * 0.80)
        closed = 0

        if current_price <= stop:
            self.state.close_position(pos_id, current_price, "stop_loss")
            closed = 1
        elif current_price >= entry * 1.20 and stop < entry:
            log.info("[TRAILING] %s stop moved to breakeven $%.3f", pos_id, entry)
        else:
            buffer = 2.0 if loc["unit"] == "F" else 1.0
            mid = (pos["bucket_low"] + pos["bucket_high"]) / 2
            if mid == -999 or mid == 999:
                return 0
            shift = abs(forecast_temp - mid)
            bucket_width = abs(pos["bucket_high"] - pos["bucket_low"])
            if shift > (bucket_width + buffer):
                self.state.close_position(pos_id, current_price, "forecast_shift")
                closed = 1

        return closed


def city_slug(name: str) -> str:
    """Convert city display name to slug."""
    return name.lower().replace(" ", "-").replace(",", "")
