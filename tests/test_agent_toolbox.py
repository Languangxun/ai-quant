"""决策工具集测试：不联网、不启动 CLI。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import toolbox


class _Search:
    def __init__(self):
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return [{
            "title": f"结果 {query[:10]}",
            "summary": "摘要",
            "date": "2026-11-05",
            "site": "测试",
        }]


def test_tool_specs_cover_gui_tools():
    box = toolbox.ToolBox({})
    names = {s["function"]["name"] for s in box.specs()}
    assert names == {
        "get_market", "get_prediction", "get_kline", "get_quote",
        "get_orderbook", "get_external_env", "get_etf", "search_news",
    }


def test_call_cap_and_unknown_tool(monkeypatch):
    monkeypatch.setattr(toolbox.market_view, "brief", lambda: "上证偏弱")
    monkeypatch.setattr(toolbox.market_view, "trend", lambda: "weak")
    box = toolbox.ToolBox({}, max_calls=1)
    first = json.loads(box.call("get_market", {}))
    assert first["market_brief"] == "上证偏弱"
    second = json.loads(box.call("get_market", {}))
    assert "上限" in second["error"]
    unknown = toolbox.ToolBox({}, max_calls=2).call("nope", {})
    assert "未知工具" in unknown
    assert len(box.trace) == 1


def test_external_env_uses_search_and_cache():
    client = _Search()
    box = toolbox.ToolBox({}, search_client=client, max_calls=8)
    out = json.loads(box.call("get_external_env", {"days": 2}))
    assert "结果" in out["results"] and out["note"]
    assert len(client.queries) == 2
    box.call("get_external_env", {"days": 2})
    assert len(client.queries) == 2  # 15 分钟缓存，不重复计费
    assert len(box.trace) == 2
    assert all(t["error"] is False for t in box.trace)


def test_search_news_with_code(monkeypatch):
    monkeypatch.setattr(
        toolbox.ToolBox, "_tool_get_quote",
        lambda self, args: json.dumps(
            {"code": "sz002050", "name": "三花智控"}, ensure_ascii=False))
    client = _Search()
    box = toolbox.ToolBox({}, search_client=client)
    out = json.loads(box.call("search_news", {"code": "sz002050"}))
    assert "三花智控" in out["query"]
    assert out["results"]


def test_prediction_tool_uses_premarket_when_code_missing():
    pre = {"predictions": [{"code": "sz002050", "t1_p50": 33}]}
    box = toolbox.ToolBox({"premarket": pre})
    out = json.loads(box.call("get_prediction", {}))
    assert out["source"] == "premarket"
    assert out["predictions"][0]["code"] == "sz002050"


def test_prediction_tool_on_demand(monkeypatch):
    from agent import predictor

    monkeypatch.setattr(
        predictor, "predict",
        lambda code, timeout=45: {"code": "sz002050", "t1_p50": 33})
    box = toolbox.ToolBox({})
    out = json.loads(box.call("get_prediction", {"code": "sz002050"}))
    assert out["prediction"]["t1_p50"] == 33


def test_prediction_tool_batch(monkeypatch):
    from agent import predictor

    monkeypatch.setattr(
        predictor, "predict",
        lambda code, timeout=45: {"code": code, "t1_p50": len(code)})
    box = toolbox.ToolBox({})
    out = json.loads(box.call(
        "get_prediction", {"codes": ["sz002050", "sh600519"]}))
    assert [p["code"] for p in out["predictions"]] == [
        "sz002050", "sh600519"]


def test_orderbook_tool(monkeypatch):
    from data.stock import cli_bridge as bridge

    monkeypatch.setattr(bridge, "order_book", lambda code, ttl=3.0: {
        "name": "三花智控", "phase": "连续竞价", "auction": False,
        "price": 33.5, "prev_close": 33.0, "open": 33.1,
        "high": 33.9, "low": 32.8, "limit_up": 36.3, "limit_down": 29.7,
        "bids": [[33.49, 120], [33.48, 80]], "asks": [[33.50, 100]],
        "bid_vol": 200, "ask_vol": 100, "wei_bi": 33.3, "time": "20261106100000",
    })
    box = toolbox.ToolBox({})
    out = json.loads(box.call("get_orderbook", {"code": "sz002050"}))
    assert out["phase"] == "连续竞价"
    assert out["bid1"] == 33.49 and out["ask1"] == 33.5
    assert out["spread"] == 0.01
    assert out["wei_bi_pct"] == 33.3


def test_etf_tool(monkeypatch):
    from data.stock import cli_bridge as bridge

    class _CLI:
        def etf_constituent_perf(self, full, limit):
            return ({"period": "2026Q3"}, [
                {"code": "sh600519", "name": "贵州茅台", "weight": 8.5,
                 "price": 1500, "chg": 1.2, "ret5": 2.3, "ret20": 5.6,
                 "vr": 1.1},
            ])

    monkeypatch.setattr(bridge, "is_etf", lambda code: True)
    monkeypatch.setattr(bridge, "load", lambda: _CLI())
    box = toolbox.ToolBox({})
    out = json.loads(box.call("get_etf", {"code": "sh510300", "limit": 5}))
    assert out["code"] == "sh510300"
    assert out["rows"][0]["weight_pct"] == 8.5
    assert out["report"] == "2026Q3"


def test_tool_errors_are_json():
    box = toolbox.ToolBox({}, max_calls=3)
    out = box.call("get_kline", {"code": ""})
    assert out.startswith('{"error"')
    assert box.trace[-1]["error"] is True
