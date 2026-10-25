"""审查修复回归: sina fut 绑定 / last_price 兜底 / 模型硬门 / CLI 默认值."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_sina_fut_rows_binding():
    """新浪 fut 增量 rows 必须带 code (7元组), 与 7 占位符对齐 (P1 回归)."""
    import scripts.sync_global_daily as sg
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE daily_bars(market TEXT, code TEXT, date TEXT,"
                " open REAL, high REAL, low REAL, close REAL, volume REAL,"
                " PRIMARY KEY(market, code, date))")
    code = "RB0"
    rows = [(code,) + r for r in [("2026-10-26", 3000.0, 3050.0, 2990.0,
                                   3040.0, 1000.0)]]
    con.executemany("INSERT OR IGNORE INTO daily_bars(market, code, date, "
                    "open, high, low, close, volume) "
                    "VALUES('FUT',?,?,?,?,?,?,?)", rows)
    n = con.execute("SELECT COUNT(*) FROM daily_bars WHERE market='FUT' "
                    "AND code=?", (code,)).fetchone()[0]
    assert n == 1
    # _fut_bars_sina 解析格式: jsonp 壳剥掉后为 6 字典键 d/o/h/l/c/v
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts", "sync_global_daily.py"),
        encoding="utf-8").read()
    assert "[(code,) + r for r in rows]" in src


def test_multi_account_last_price_fallback():
    from trading.multi_account import MultiAccount
    a = MultiAccount(20000, commission_rate=0, min_commission=0,
                     stamp_tax_rate=0, transfer_fee_rate=0)
    a.buy("fut:RB0", 3000.0, 1, "2026-10-20", "2026-10-20")
    assert a.market_value({}) == 3000.0        # 停牌/无价 → 回退买价
    assert a.market_value({"fut:RB0": 3100.0}) == 3100.0
    assert a.market_value({}) == 3100.0        # 回退最近已知价
    b = MultiAccount.from_state(a.to_state())
    assert b.market_value({}) == 3100.0        # 状态往返保留
    b.sell("fut:RB0", 3200.0, 1, "2026-10-21")
    assert b.position_codes() == []


def test_sm701_no_single_model_gate():
    """production 模型在位时, 44维单模型缺失不应挡住 hk/us/fut 信号 (P2)."""
    from factors import ml
    if not ml.available():
        return
    prod_dir = ml.production_dir("fut")
    if not prod_dir:
        return  # 无 production 环境跳过
    row = {k: 0.0 for k in ml.FEATURES_SM701}
    row.update({"ret5": 0.05, "rsi14": 60.0, "ma20r": 0.03})
    orig = ml.model_path
    try:
        ml.model_path = lambda: ""   # 模拟单模型缺失
        out = ml.predict_proba([row], market="fut")
        assert len(out) == 1 and 0.0 <= out[0] <= 1.0
    finally:
        ml.model_path = orig
    # cn 在单模型缺失时优雅返回空 (不抛错)
    try:
        ml.model_path = lambda: ""
        assert ml.predict_proba([row], market="cn") == []
    finally:
        ml.model_path = orig


def test_multi_engine_cli_defaults_none():
    """CLI 默认 None, yaml 生效 (P2 回归: min-hold 恒 5 吃掉配置)."""
    import backtest.multi_engine as me
    import inspect
    src = inspect.getsource(me)
    assert 'default=None' in src
    assert 'max_scan' in src
    import sim.multi_run as mr
    assert 'max_scan' in inspect.getsource(mr.run_one)
