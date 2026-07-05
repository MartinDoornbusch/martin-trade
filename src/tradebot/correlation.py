"""Pearson-correlatie op candle-returns. Gebruikt als risk-gate (blokkeert dubbel
gecorreleerde posities) en in het instap-advies. Puur, geen I/O."""
from __future__ import annotations

import math


def returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]


def pearson(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 10:  # te weinig datapunten voor een zinvolle schatting
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


def correlation_from_closes(closes_a: list[float], closes_b: list[float],
                            lookback: int = 60) -> float | None:
    ra, rb = returns(closes_a[-lookback - 1:]), returns(closes_b[-lookback - 1:])
    return pearson(ra, rb)
