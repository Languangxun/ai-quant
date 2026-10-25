"""LLM 余额不足时的纯代码降级测试（无网络依赖）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.stock_decision import StockDecisionMaker, code_only_decision
from models.deepseek import is_network_error, is_quota_error


class QuotaError(Exception):
    status_code = 402


def _ctx():
    return {
        "candidates": [
            {"code": "sh600000", "score": 12},
            {"code": "sh601328", "score": 10},
            {"code": "sh600900", "score": 9},
            {"code": "sz000725", "score": 8},
            {"code": "sh510300", "score": 7},
            {"code": "sh601668", "score": 6},
        ],
        "positions": [
            {"code": "sh600000", "sellable": 100, "pnl_pct": 1.0},
            {"code": "sh601668", "sellable": 0, "pnl_pct": -8.0},
            {"code": "sh600999", "sellable": 100, "pnl_pct": 1.0},
        ],
        "account": {
            "max_positions": 5,
            "max_position_pct": 20,
            "bought_today": [],
            "sold_today": [],
        },
    }


def test_is_quota_error():
    assert is_quota_error(QuotaError("Insufficient Balance"))
    assert is_quota_error(RuntimeError("Error code: 402 - Insufficient Balance"))
    assert not is_quota_error(RuntimeError("JSON 解析失败"))


def test_code_only_decision_rotates_and_fills_slots():
    out = code_only_decision(_ctx(), "余额不足")
    assert out["degraded"] is True
    orders = {o["code"]: o for o in out["orders"]}
    assert orders["sh600999"]["action"] == "SELL"
    assert "sh601668" not in orders
    buys = [o for o in out["orders"] if o["action"] == "BUY"]
    assert [o["code"] for o in buys] == ["sh601328", "sh600900", "sz000725"]
    assert all(o["position"] == 20.0 for o in buys)


def test_code_only_decision_empty_candidates_holds():
    ctx = _ctx()
    ctx["candidates"] = []
    out = code_only_decision(ctx, "无额度")
    assert out["orders"] == []
    assert out["degraded"] is True


def test_maker_degrades_without_api_key(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("DEEPSEEK_API_KEY 未配置")

    monkeypatch.setattr("agent.stock_decision.DeepSeekModel", boom)
    maker = StockDecisionMaker()
    result, err = maker.decide(_ctx())
    assert err is None
    assert result["degraded"] is True
    assert result["orders"]


def test_maker_degrades_on_quota_error(monkeypatch):
    maker = StockDecisionMaker()
    maker.init_error = None

    class Client:
        model = "deepseek-flash"

        def analyze(self, context):
            raise QuotaError("Insufficient Balance")

    maker.client = Client()
    result, err = maker.decide(_ctx())
    assert err is None
    assert result["degraded"] is True
    assert result["orders"]


def test_is_network_error():
    assert is_network_error(RuntimeError("DeepSeek 决策失败: Connection error."))
    assert is_network_error(OSError("Name or service not known"))
    assert not is_network_error(RuntimeError("JSON 解析失败"))


def test_maker_degrades_on_connection_error(monkeypatch):
    maker = StockDecisionMaker()
    maker.init_error = None

    class Client:
        model = "deepseek-flash"

        def analyze(self, context):
            raise RuntimeError("DeepSeek 决策失败（已重试 2 次）: Connection error.")

    maker.client = Client()
    result, err = maker.decide(_ctx())
    assert err is None
    assert result["degraded"] is True
    assert result["orders"]
