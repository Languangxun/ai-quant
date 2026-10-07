def test_unified_counts():
    from data import unified as u
    try:
        c = u.counts()
    except FileNotFoundError:
        import pytest
        pytest.skip("无缓存库")
    assert "cn" in c and c["cn"]["rows"] > 1000000
    assert "hk" in c and c["hk"]["codes"] >= 2000
    assert "us" in c and c["us"]["codes"] >= 8000
    assert "crypto" in c and c["crypto"]["codes"] >= 15


def test_unified_bars():
    from data import unified as u
    try:
        bars = u.get_bars("hk:00700", tail=5)
    except FileNotFoundError:
        import pytest
        pytest.skip("无缓存库")
    assert len(bars) == 5 and bars[-1].close > 0
    us = u.get_bars("us:AAPL", tail=3)
    assert len(us) == 3 and us[-1].close > 0
    cr = u.get_bars("crypto:BTC/USDT", tail=3)
    assert len(cr) == 3 and cr[-1].close > 0
    cn = u.get_bars("sh600000", tail=3)
    assert len(cn) == 3 and cn[-1].close > 0
