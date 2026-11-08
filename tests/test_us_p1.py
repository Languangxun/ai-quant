"""美股 P1 48维特征 builder 单测 (不依赖模型/数据库)."""
import math

from core.types import Bar
from factors import us_p1


def _mk(code, n=300, drift=0.0005):
    bars = []
    px = 100.0
    for i in range(n):
        px *= 1.0 + drift + (0.01 if i % 7 == 0 else -0.004)
        bars.append(Bar(code=code, date=f"2026-{(i // 28) % 12 + 1:02d}-"
                           f"{i % 28 + 1:02d}", open=px * 0.995,
                        high=px * 1.01, low=px * 0.99, close=px,
                        vol=1_000_000 + i * 1000, raw_close=px * 0.98))
    return bars


def test_bar_raw_close_default():
    b = Bar(code="us:AAPL", date="2026-11-06", close=100.0)
    assert b.raw_close == 0.0


def test_build_rows_shape_and_ranks():
    rows = us_p1.build_rows({
        "us:AAA": _mk("us:AAA", drift=0.001),
        "us:BBB": _mk("us:BBB", drift=0.0),
        "us:CCC": _mk("us:CCC", drift=-0.001),
    })
    assert len(rows) == 3
    for code, row in rows.items():
        assert set(row) == set(us_p1.FEATURES_US_P1)
        assert all(isinstance(v, float) for v in row.values())
        # 长周期/波动率/美元成交额均应有值 (300根 > 260 warmup)
        for k in ("ret250", "vol120", "dvolr20", "logvol",
                  "r_ret60", "r_mom12_1", "r_dist_high250"):
            assert math.isfinite(row[k]), (code, k, row[k])
        for k in ("r_ret5", "r_ret20", "r_ret60", "r_ma20r"):
            assert 0.0 <= row[k] <= 1.0
    # 动量最强 -> ret60 rank 最高
    assert rows["us:AAA"]["r_ret60"] > rows["us:BBB"]["r_ret60"] > \
        rows["us:CCC"]["r_ret60"]


def test_short_bars_dropped():
    assert us_p1.build_rows({"us:AAA": _mk("us:AAA", n=120)}) == {}
