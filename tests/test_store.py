def test_store_counts_smoke():
    from data import store as st
    try:
        c = st.counts()
    except Exception as e:
        # Pi 上无库时跳过 (CI 容忍)
        import pytest
        pytest.skip(f"无缓存库: {e}")
    assert c["codes"] >= 0 and c["rows"] >= 0


def test_technical_compute():
    from factors import technical as ft
    closes = [10 + i * 0.1 for i in range(30)]
    out = ft.compute(closes)
    assert "ma5" in out and "rsi14" in out
    assert len(out["ma5"]) == len(closes)
