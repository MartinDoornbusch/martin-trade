from tradebot.correlation import correlation_from_closes, pearson, returns


def test_perfect_positive_correlation():
    a = [float(i) for i in range(1, 40)]
    b = [float(i) * 2 for i in range(1, 40)]
    assert pearson(returns(a), returns(b)) > 0.99


def test_negative_correlation():
    import random
    random.seed(7)
    a = [100.0]
    for _ in range(50):
        a.append(a[-1] * (1 + random.gauss(0, 0.01)))
    b = [300.0 - x for x in a]  # gespiegelde walk: returns bewegen tegengesteld
    c = correlation_from_closes(a, b)
    assert c < -0.9


def test_too_few_points_returns_none():
    assert pearson([0.1, 0.2], [0.1, 0.2]) is None


def test_flat_series_returns_none():
    a = [100.0] * 40
    b = [float(i) for i in range(1, 41)]
    assert correlation_from_closes(a, b) is None
