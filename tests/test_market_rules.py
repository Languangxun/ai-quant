"""MarketModel / 风控 / ETF 费用 / T+0 / 订单状态 单元测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.stock_rules import StockRiskManager
from trading.instruments import MarketModel, price_limit_pct
from trading.orders import OrderJournal, STATUS_FILLED, STATUS_NO_CHANGE
from trading.stock_account import StockAccount
from trading.stock_executor import StockExecutor


# ---------- MarketModel ----------

def test_stock_rules():
    m = MarketModel()
    inst = m.classify("sz000725", "京东方A")
    assert inst.kind == "stock" and inst.t_plus_1
    assert inst.limit_pct == 0.10
    assert inst.stamp_tax and inst.transfer_fee
    assert m.classify("sz300750", "宁德时代").limit_pct == 0.20
    assert m.classify("sh688981", "中芯国际").limit_pct == 0.20


def test_etf_default_t_plus_1_and_no_tax():
    inst = MarketModel().classify("sh510300", "沪深300ETF")
    assert inst.kind == "etf" and inst.t_plus_1
    assert inst.limit_pct == 0.10
    assert not inst.stamp_tax and not inst.transfer_fee


def test_etf_t0_by_prefix_and_keyword():
    m = MarketModel()
    assert m.classify("sh513100", "").t0                  # 跨境（代码前缀）
    assert m.classify("sh511010", "").t0                  # 国债（代码前缀）
    assert m.classify("sh518880", "").t0                  # 黄金（代码前缀）
    assert m.classify("sz159920", "恒生ETF").t0            # 跨境（名称关键词）
    assert not m.classify("sz159915", "创业板ETF").t0      # 股票型 ETF 仍 T+1


def test_etf_wide_limit():
    m = MarketModel()
    assert m.classify("sz159915", "创业板ETF").limit_pct == 0.20
    assert m.classify("sh588000", "科创50ETF").limit_pct == 0.20
    assert price_limit_pct("sz159915", "创业板ETF") == 0.20


def test_market_model_config_override():
    cfg = {"lot_size": 100,
           "market": {"etf": {"t0_codes": ["sz159941"],
                              "t0_prefixes": [],
                              "t0_keywords": [],
                              "limit_pct_overrides": {"sz159941": 0.20}}}}
    m = MarketModel(cfg)
    inst = m.classify("sz159941", "纳指ETF")
    assert inst.t0 and inst.limit_pct == 0.20
    assert m.classify("sh513100", "").t0 is False   # 前缀被覆盖为空


# ---------- 账户按标的计费 ----------

def test_etf_sell_fee_without_stamp_tax():
    acc = StockAccount(100000)
    acc.buy("sh510300", 4.0, 100, "2026-09-24", "2026-09-24",
            transfer_fee_rate=0.0)
    t = acc.sell("sh510300", 4.0, 100, "2026-09-24",
                 stamp_tax_rate=0.0, transfer_fee_rate=0.0)
    assert abs(t.fee - 5.0) < 1e-9          # 仅最低佣金
    assert abs(acc.fees - 10.0) < 1e-9


# ---------- 风控层 ----------

def test_risk_min_order_and_position_cap():
    acc = StockAccount(20000)
    rm = StockRiskManager(acc, min_order_amount=5000, min_order_pct=5)
    assert rm.min_amount_for(20000) == 1000
    d = rm.check_buy("sh600900", 28.36, "2026-09-28", target_pct=2,
                     prices={"sh600900": 28.36})
    assert not d.allowed and d.reason == "金额不足"
    acc2 = StockAccount(20000)
    for i in range(5):
        acc2.buy(f"sh60000{i}", 10.0, 100, "2026-09-24", "2026-09-25")
    rm2 = StockRiskManager(acc2)
    d2 = rm2.check_buy("sz000001", 10.0, "2026-09-28", target_pct=10,
                       prices={"sz000001": 10.0})
    assert not d2.allowed and d2.reason == "持仓数已满"


# ---------- 执行器：T+0 / T+1 / 订单状态 ----------

def test_executor_etf_t0_and_stock_t_plus_1():
    acc = StockAccount(20000)
    ex = StockExecutor(acc, max_positions=5, max_position_pct=20,
                       min_order_amount=5000, min_order_pct=5)
    names = {"sh513100": "纳指ETF", "sh600900": "长江电力"}
    prices = {"sh513100": 1.0, "sh600900": 28.36}
    prev = dict(prices)
    t = ex.execute_order(
        {"code": "sh513100", "action": "BUY", "position": 10}, prices,
        "2026-09-28", prev_closes=prev, names=names)
    assert not isinstance(t, str) and t.side == "BUY"
    r = ex.execute_order(
        {"code": "sh513100", "action": "SELL"}, prices, "2026-09-28",
        prev_closes=prev, names=names)
    assert not isinstance(r, str) and r.side == "SELL"      # T+0 当日可卖

    t2 = ex.execute_order(
        {"code": "sh600900", "action": "BUY", "position": 10}, prices,
        "2026-09-28", prev_closes=prev, names=names)
    assert not isinstance(t2, str) and t2.side == "BUY"
    r2 = ex.execute_order(
        {"code": "sh600900", "action": "SELL"}, prices, "2026-09-28",
        prev_closes=prev, names=names)
    assert isinstance(r2, str) and "无可卖份额" in r2        # T+1 当日不可卖


def test_executor_journal_statuses():
    acc = StockAccount(20000)
    journal = OrderJournal()
    ex = StockExecutor(acc, market=MarketModel(), journal=journal,
                       min_order_amount=5000, min_order_pct=5)
    ex.execute_order({"code": "sh510300", "action": "HOLD"}, {}, "2026-09-28")
    ex.execute_order({"code": "sh510300", "action": "BUY", "position": 10},
                     {"sh510300": 4.0}, "2026-09-28",
                     prev_closes={"sh510300": 3.99},
                     names={"sh510300": "沪深300ETF"})
    assert journal.records[0]["status"] == "HOLD"
    assert journal.records[1]["status"] == STATUS_FILLED
    assert journal.records[1]["kind"] == "etf"
    assert journal.records[1]["t0"] is False
