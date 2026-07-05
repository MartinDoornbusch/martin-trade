"""Technical indicators. Pure functions, no AI involved (deterministic by design)."""
from __future__ import annotations

import numpy as np


def ema(values: list[float], period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    alpha = 2.0 / (period + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: list[float], period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    arr = np.asarray(values, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    out = np.full(len(arr), np.nan)
    if len(arr) <= period:
        return out
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss else np.inf))
    for i in range(period + 1, len(arr)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rs = avg_gain / avg_loss if avg_loss else np.inf
        out[i] = 100 - 100 / (1 + rs)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(values, fast) - ema(values, slow)
    sig = ema(list(line), signal)
    return line, sig, line - sig  # macd line, signal line, histogram


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> np.ndarray:
    h, lo, c = (np.asarray(x, dtype=float) for x in (highs, lows, closes))
    prev_close = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_close), np.abs(lo - prev_close)))
    out = np.full(len(c), np.nan)
    if len(c) < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(c)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger(values: list[float], period: int = 20, num_std: float = 2.0):
    arr = np.asarray(values, dtype=float)
    mid = np.full(len(arr), np.nan)
    upper = np.full(len(arr), np.nan)
    lower = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        window = arr[i - period + 1: i + 1]
        m, s = window.mean(), window.std()
        mid[i], upper[i], lower[i] = m, m + num_std * s, m - num_std * s
    return mid, upper, lower
