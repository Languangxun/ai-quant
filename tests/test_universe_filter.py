"""跨市场股票池质量过滤测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.types import Bar
from data.universe_filter import liquid_top


def _bars(code, n=130):
    return [Bar(code=code, date=f"d{i:03d}", open=10.0 + i * 0.01,
                high=10.1 + i * 0.01, low=9.9 + i * 0.01,
                close=10.0 + i * 0.01, vol=100000) for i in range(n)]


def test_hk_excludes_warrants_and_unnamed():
    cands = [("hk:00700", "腾讯控股"), ("hk:49880", ""),
             ("hk:41533", "未来资产"), ("hk:08001", "GEM 股票")]
    out = liquid_top("hk", cands, lambda c, tail=150: _bars(c), limit=10)
    assert sorted(c for c, _ in out) == ["hk:00700", "hk:08001"]
