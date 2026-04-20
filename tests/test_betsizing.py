import pytest, math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.betsizing import bucket_prob, calc_ev, calc_kelly, bet_size, norm_cdf

def test_bucket_prob_at_center():
    p = bucket_prob(80.5, 80, 81, sigma=2.0)
    assert 0.15 < p < 0.25  # 1-degree bucket at center with sigma=2 gives ~0.197

def test_bucket_prob_wide_range():
    p = bucket_prob(80.0, 70, 90, sigma=2.0)
    assert p > 0.9  # very wide range should be near 1.0

def test_bucket_prob_outside_range():
    p = bucket_prob(50.0, 80, 81, sigma=2.0)
    assert p < 0.01  # far outside should be near 0

def test_bucket_prob_exact_point():
    p = bucket_prob(80.0, 80, 80, sigma=2.0)
    assert 0.1 < p < 0.9  # single degree bucket

def test_ev_buy_below_true_prob():
    ev = calc_ev(0.8, 0.6)
    assert ev > 0  # positive EV when we have edge

def test_ev_buy_at_true_prob():
    ev = calc_ev(0.5, 0.5)
    assert ev == 0.0

def test_ev_buy_above_true_prob():
    ev = calc_ev(0.3, 0.6)
    assert ev < 0

def test_ev_edge_cases():
    assert calc_ev(0.5, 0.0) == 0.0
    assert calc_ev(0.5, 1.0) == 0.0
    assert calc_ev(0.5, 0.5) == 0.0

def test_kelly_basic():
    k = calc_kelly(0.7, 0.5)
    assert 0.05 < k < 0.5

def test_kelly_edge_cases():
    assert calc_kelly(0.5, 0.0) == 0.0
    assert calc_kelly(0.5, 1.0) == 0.0
    assert calc_kelly(1.0, 0.3) == 1.0  # certain win with positive odds -> full Kelly = 1.0
    assert calc_kelly(0.0, 0.5) == 0.0

def test_bet_size_caps_at_max_bet():
    size = bet_size(kelly=1.0, balance=100.0, max_bet=20.0)
    assert size == 20.0

def test_bet_size_min_bet():
    size = bet_size(kelly=0.004, balance=100.0, max_bet=20.0)  # raw=0.4 < min_bet=0.5
    assert size == 0.0  # below $0.50 minimum

def test_bet_size_respects_balance():
    size = bet_size(kelly=0.5, balance=5.0, max_bet=20.0)
    assert size <= 5.0

def test_norm_cdf():
    assert abs(norm_cdf(0) - 0.5) < 0.001
    assert abs(norm_cdf(1.96) - 0.975) < 0.01