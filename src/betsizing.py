"""Kelly Criterion bet sizing and bucket probability math."""
import math

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast: float, t_low: float, t_high: float, sigma: float = 2.0) -> float:
    """
    Probability that actual temperature falls in [t_low, t_high]
    given a Gaussian-distributed forecast error with std=sigma.
    """
    if t_low == t_high:
        # Exact point — single degree bucket, use 0.5-degree band
        z = (forecast - t_low) / sigma
        return norm_cdf(z + 0.5/sigma) - norm_cdf(z - 0.5/sigma)
    # Range bucket
    z_low = (t_low - forecast) / sigma
    z_high = (t_high - forecast) / sigma
    return norm_cdf(z_high) - norm_cdf(z_low)

def calc_ev(p: float, price: float, ev_cap: float = 5.0) -> float:
    """
    Expected value of buying a YES token at `price` when true prob = p.
    ev_cap prevents single-degree/single-bucket overconfidence from creating
    explosive Kelly sizes on near-zero-priced contracts (Singapore effect).
    """
    if price <= 0 or price >= 1:
        return 0.0
    ev = p * (1.0 / price - 1.0) - (1.0 - p)
    return max(0.0, min(ev, ev_cap))

def calc_kelly(p: float, price: float) -> float:
    """
    Full Kelly fraction for a binary outcome.
    b = payout multiplier (1/price - 1)
    f = (p*b - (1-p)) / b
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    if b <= 0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, min(f, 1.0))

def bet_size(kelly: float, balance: float, max_bet: float, min_bet: float = 0.50) -> float:
    """Calculate bet size given Kelly fraction and constraints."""
    raw = balance * kelly
    if raw < min_bet:
        return 0.0
    return min(raw, max_bet)


def calc_kelly_penny(p: float, price: float, kelly_fraction: float) -> float:
    """
    Hybrid Kelly sizing for ColdMath-style penny entries.

    ColdMath entered at $0.001-$0.015 (penny stocks) and won 95% of the time.
    His actual Kelly fraction would have been ~0.01% (very small), but he scaled
    to $5K-$13K per trade. His edge came from:
    1. Entry price: $0.001-$0.015 (genuinely cheap, market underestimates)
    2. Position size: 3-5% of bankroll regardless of Kelly
    3. Win rate: 95% (extreme cold events are predictable)

    For penny entries (price ≤ $0.05):
    - Use a fixed min_kelly = 0.03 (3% of balance) to capture the ColdMath effect
    - Scale by p to reward higher conviction
    For normal entries:
    - Use standard Kelly formula
    """
    if price <= 0 or price >= 1:
        return 0.0

    if price <= 0.05:
        # Penny entry: use fixed-min Kelly scaled by conviction
        kelly = 0.03 * p  # 3% base × probability
        kelly = kelly * kelly_fraction
        return max(0.0, min(kelly, 1.0))
    else:
        # Normal Kelly
        b = 1.0 / price - 1.0
        if b <= 0:
            return 0.0
        f = (p * b - (1.0 - p)) / b
        return max(0.0, min(f * kelly_fraction, 1.0))