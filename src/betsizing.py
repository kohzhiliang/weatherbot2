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

def calc_ev(p: float, price: float) -> float:
    """Expected value of buying a YES token at `price` when true prob = p."""
    if price <= 0 or price >= 1:
        return 0.0
    return p * (1.0 / price - 1.0) - (1.0 - p)

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