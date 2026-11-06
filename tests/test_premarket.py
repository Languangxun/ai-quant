"""盘前资料模块测试（不发起真实搜索/LLM 请求）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import premarket
from scripts import premarket_research as pr


def _item(title="标题", url="https://example.com/a", summary="摘要"):
    return {"title": title, "url": url, "summary": summary,
            "date": "2026-11-05", "site": "站点"}


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    doc = {"date": "2026-11-05", "generated_at": "2026-11-05T08:30:00",
           "queries": ["q"], "brief": "外盘/宏观：...",
           "items": [_item(summary="x" * 900)]}
    path = premarket.save(doc)
    assert os.path.exists(path)
    loaded = premarket.load("2026-11-05")
    assert loaded["brief"].startswith("外盘/宏观")
    ctx = premarket.load_for_decision("2026-11-05", max_chars=100)
    assert ctx and "指令" in ctx["note"]
    assert ctx["items"][0]["summary"] == "x" * 600


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    assert premarket.load_for_decision("2026-11-05") is None


def test_query_format():
    q = pr._query("A股 盘前 新闻", 2)
    assert q.endswith("lang:zh")
    assert " after:" in q and len(q) <= 128


def test_collect_dedupes_and_caps():
    class Fake:
        def search(self, query):
            return [_item(url="https://a"), _item(url="https://a"),
                    _item(url="https://b")]

    items = pr._collect(Fake(), ["q1", "q2"], 2, 5)
    assert [x["url"] for x in items] == ["https://a", "https://b"]


def test_run_saves_file(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    monkeypatch.setattr("models.web_search.build_from_config",
                        lambda: object())
    monkeypatch.setattr(pr, "_collect",
                        lambda *a, **k: [_item(), _item(url="https://b")])
    assert pr.run(force=True, use_llm=False, use_predict=False) == 0
    docs = [p for p in os.listdir(tmp_path) if p.startswith("premarket-")]
    assert docs
    doc = json.load(open(os.path.join(tmp_path, docs[0]), encoding="utf-8"))
    assert len(doc["items"]) == 2
    assert doc["brief"] == ""


def test_load_auction(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    path = premarket.auction_path("2026-11-06")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": "2026-11-06", "label": "0926",
                   "books": {"sz002050": {"name": "三花智控",
                                          "phase": "集合竞价",
                                          "bid1": 33.8, "ask1": 34.0}}},
                  f, ensure_ascii=False)
    doc = premarket.load_auction("2026-11-06")
    assert doc and doc["books"]["sz002050"]["phase"] == "集合竞价"


def test_intraday_save_and_load_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    doc = {
        "date": "2026-11-06", "label": "1230",
        "generated_at": "2026-11-06T12:30:00",
        "event_watch": {"brief": "午休事件", "items": [], "salience": 0.7},
        "event_risk": {"level": "elevated"},
    }
    path = premarket.save_intraday(doc)
    assert os.path.exists(path)
    got = premarket.load_latest_intraday("2026-11-06")
    assert got and got["label"] == "1230"
    assert got["event_watch"]["brief"] == "午休事件"


def test_prediction_summary_compact():
    res = {
        "full_code": "sz002050", "quote": {"name": "三花智控", "price": 33.5},
        "sector_name": "家电零部件", "t_pred": {"cl": {10: 31, 50: 33, 90: 35},
                                               "up_prob": 0.62},
        "t5_pred": {"cl": {25: 31, 50: 34, 75: 37}, "up_prob": 0.55},
        "multi_pred": [{"day": 1, "cum_cl": 0.01, "price_cl": 33.8},
                       {"day": 5, "cum_cl": 0.05, "price_cl": 35.1}],
        "action": {"score": 8, "verdict": "偏多"},
        "samples": [{"n1_cl": 0.01}, {"n1_cl": -0.02}],
        "signals": [(1, "2026-11-04", "BUY", "MACD金叉")],
        "chips": {"avg_cost": 33, "profit": 0.4, "sup": 32, "res": 35},
        "vr_now": 1.2, "cur_regime": "平量",
    }
    p = pr._prediction_summary(res)
    assert p["code"] == "sz002050" and p["t1_p50"] == 33
    assert p["t1_up_prob"] == 62.0 and p["t5_up_prob"] == 55.0
    assert p["signals"][0]["type"] == "BUY"
    assert p["samples"]["up_rate"] == 50.0


def test_run_with_predictions(tmp_path, monkeypatch):
    monkeypatch.setattr(premarket, "DAILY_DIR", str(tmp_path))
    monkeypatch.setattr("models.web_search.build_from_config",
                        lambda: object())
    monkeypatch.setattr(pr, "_collect", lambda *a, **k: [_item()])
    monkeypatch.setattr(pr, "_market_brief", lambda: "上证在MA20下方")
    monkeypatch.setattr(pr, "_build_predictions", lambda *a, **k: [{
        "code": "sz002050", "name": "三花智控", "sector": "家电",
        "score": 8, "t1_p50": 33, "t1_up_prob": 62}])
    assert pr.run(force=True, use_llm=False) == 0
    files = [p for p in os.listdir(tmp_path)
             if p.startswith("premarket-") and p.endswith(".json")]
    assert files
    doc = json.load(open(os.path.join(tmp_path, files[0]), encoding="utf-8"))
    assert doc["predictions"][0]["code"] == "sz002050"
    assert "MA20" in doc["market_brief"]
