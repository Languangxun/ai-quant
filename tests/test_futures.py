"""期货 (fut) 接入测试: 编码/规则/市场识别/成分过滤/多市场配置."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.instruments import MarketModel
from strategies.builtin import _market_of
from data.unified import _split
from data import universe_filter as uf


def test_classify_fut():
    m = MarketModel()
    i = m.classify("fut:RB0", "螺纹钢")
    assert i.kind == "fut"
    assert i.t0 is True                       # T+0 当日可平
    assert i.limit_pct == 1.0                 # 涨跌停校验跳过 (未建模)
    assert i.stamp_tax is False               # 无印花税
    assert i.transfer_fee is False            # 无过户费
    assert i.lot_size == 1                    # 按手整数


def test_classify_fut_case_insensitive():
    m = MarketModel()
    assert m.classify("FUT:rb0").kind == "fut"
    assert m.classify("FUT:RB0").kind == "fut"


def test_unified_split():
    assert _split("fut:RB0") == ("fut", "RB0")
    assert _split("fut:rb0")[0] == "fut"
    assert _split("sh600000") == ("cn", "sh600000")
    assert _split("hk:00700") == ("hk", "00700")


def test_market_of():
    assert _market_of("fut:RB0") == "fut"
    assert _market_of("hk:00700") == "hk"
    assert _market_of("us:AAPL") == "us"
    assert _market_of("sz000725") == "cn"


def test_universe_filter_fut():
    # fut: 无最小价 (点价差异大), 换月跳空不按脏数据剔除, 最小120根
    assert uf.MIN_BARS.get("fut") == 120
    assert uf.MIN_PRICE.get("fut") == 0.0
    assert uf.MAX_RET.get("fut") > 0.5
    assert uf.MIN_BARS.get("fut", 120) == 120


def test_markets_yaml_fut():
    import yaml
    p = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config", "markets.yaml")
    with open(p, encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("markets") or {}
    assert "fut" in cfg, "markets.yaml 缺 fut 段"
    fut = cfg["fut"]
    assert fut.get("enabled") in (True, "true")
    assert (fut.get("fees") or {}).get("stamp_tax_rate", 0) == 0.0
    assert float((fut.get("sm701") or {}).get("threshold", 0)) == 0.55


def test_unified_fut_bars_and_universe():
    """需要 data/global_daily.db (FUT 已入库), 缺库则跳过."""
    from data import unified as u
    try:
        if "fut" not in u.counts():
            return
    except Exception:
        return
    uni = u.universe("fut")
    assert len(uni) >= 60
    bars = u.get_bars("fut:RB0", tail=10)
    assert bars and bars[-1].close > 0
    assert u.get_bars("fut:nope", tail=5) == []


def test_multi_run_markets_list():
    import sim.multi_run as mr
    import inspect
    src = inspect.getsource(mr.main)
    assert '"fut"' in src and '"crypto"' in src
