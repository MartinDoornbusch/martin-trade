import numpy as np

from tradebot.indicators import atr, bollinger, ema, macd, rsi


def test_ema_converges_to_constant():
    out = ema([10.0] * 50, 12)
    assert abs(out[-1] - 10.0) < 1e-9


def test_ema_follows_trend():
    values = list(range(1, 101))
    out = ema([float(v) for v in values], 12)
    assert out[-1] < 100  # lags behind
    assert out[-1] > 90


def test_rsi_bounds_and_direction():
    up = [float(i) for i in range(1, 40)]
    down = [float(40 - i) for i in range(1, 40)]
    assert rsi(up, 14)[-1] > 95
    assert rsi(down, 14)[-1] < 5


def test_rsi_flat_no_nan_after_warmup():
    out = rsi([5.0] * 30, 14)
    assert not np.isnan(out[-1])


def test_macd_shapes():
    line, sig, hist = macd([float(i) for i in range(60)])
    assert len(line) == len(sig) == len(hist) == 60
    np.testing.assert_allclose(hist, line - sig)


def test_atr_positive_with_range():
    highs = [11.0] * 30
    lows = [9.0] * 30
    closes = [10.0] * 30
    out = atr(highs, lows, closes, 14)
    assert abs(out[-1] - 2.0) < 1e-6


def test_bollinger_ordering():
    values = [10 + np.sin(i / 3) for i in range(50)]
    mid, upper, lower = bollinger(values)
    assert lower[-1] < mid[-1] < upper[-1]
