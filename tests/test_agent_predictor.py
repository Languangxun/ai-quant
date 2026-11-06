"""stock_predict CLI 预测封装的单元测试（不启动 CLI / 不发网络）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import predictor


def _res():
    return {
        "full_code": "sz002050",
        "quote": {"name": "三花智控", "price": 33.5},
        "sector_name": "家电零部件",
        "t_pred": {"cl": {10: 31, 50: 33, 90: 35}, "up_prob": 0.62,
                   "confidence": {"score": 0.7, "level": "高",
                                  "total_samples": 10}},
        "t5_pred": {"cl": {25: 31, 50: 34, 75: 37}, "up_prob": 0.55},
        "multi_pred": [
            {"day": 1, "cum_cl": 0.01, "price_cl": 33.8,
             "up_prob": 0.6, "cl": {50: 0.01}, "hi": {75: 0.02},
             "lo": {25: -0.01}},
            {"day": 3, "cum_cl": 0.02, "price_cl": 34.2},
            {"day": 5, "cum_cl": 0.05, "price_cl": 35.1},
            {"day": 10, "cum_cl": 0.08, "price_cl": 36.2,
             "up_prob": 0.58, "cl": {50: 0.08}, "hi": {75: 0.10},
             "lo": {25: 0.03}},
        ],
        "ghosts": [{"date": "T+5预测", "open": 33.5, "close": 35.1,
                    "high": 36.0, "low": 33.0}],
        "action": {"score": 8, "verdict": "偏多"},
        "samples": [{"n1_cl": 0.01}, {"n1_cl": -0.02}],
        "signals": [(1, "2026-11-04", "BUY", "MACD金叉")],
        "chips": {"avg_cost": 33, "profit": 0.4, "sup": 32, "res": 35},
        "vr_now": 1.2,
        "cur_regime": "平量",
    }


def test_prediction_summary_compact():
    p = predictor._prediction_summary(_res())
    assert p["code"] == "sz002050"
    assert p["name"] == "三花智控"
    assert p["t1_p50"] == 33
    assert p["t1_p10"] == 31 and p["t1_p90"] == 35
    assert p["t1_up_prob"] == 62.0
    assert p["t5_up_prob"] == 55.0
    assert p["samples"]["up_rate"] == 50.0
    assert p["signals"][0]["type"] == "BUY"
    assert [x["day"] for x in p["multi"]] == [1, 3, 5, 10]
    assert p["t10_p50"] == 36.2 and p["t10_up_prob"] == 58.0
    assert p["multi"][0]["up_prob"] == 60.0
    assert p["ghosts"][0]["close"] == 35.1
    assert p["confidence"]["level"] == "高"


def test_format_prediction():
    text = predictor.format_prediction(
        predictor._prediction_summary(_res()))
    assert "sz002050" in text and "T+1中位33" in text
    assert "上行概率62.0%" in text


def test_normalize_code_prefix_passthrough():
    assert predictor.normalize_code("sz002050") == "sz002050"
    assert predictor.normalize_code("SH600519") == "sh600519"
    assert predictor.normalize_code("000725.SZ") == "sz000725"


def test_predict_cache_hits_and_disk(tmp_path, monkeypatch):
    calls = []

    def fake_analyze(code, timeout):
        calls.append(code)
        return _res()

    monkeypatch.setattr(predictor, "_analyze_capture", fake_analyze)
    monkeypatch.setattr(predictor, "CACHE_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(predictor, "_MEM", {})
    monkeypatch.setattr(predictor, "_LOCK", predictor.threading.Lock())

    a = predictor.predict("sz002050", force=True)
    b = predictor.predict("sz002050")
    assert a["t1_p50"] == 33 and b["t1_p50"] == 33
    assert calls == ["sz002050"]
    assert os.path.exists(str(tmp_path / "p.json"))


def test_predict_many_skips_failure(monkeypatch):
    def fake(code, timeout, **kw):
        if code.endswith("000001"):
            raise RuntimeError("boom")
        return predictor._prediction_summary(_res())

    monkeypatch.setattr(predictor, "predict", fake)
    out = predictor.predict_many(["sz000001", "sz002050"])
    assert [p["code"] for p in out] == ["sz002050"]
