"""板块波动监控测试（不联网）。"""
import json
import os
import sys
import time
from datetime import datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import volatility_watch as vw


def _snapshot(pct=0.5, amp=0.9, amount=1_000_000.0, code="BK0475",
              name="银行"):
    return [{"code": code, "name": name, "price": 100.0, "pct": pct,
             "high": 101.5 if amp else 100.2, "low": 101.5 - amp * 1.0,
             "open": 100.1, "prev_close": 100.0, "volume": 1e5,
             "amount": amount, "amp": amp}]


def _bars(n=60, last_tr=None, last_ratio=None, base_close=100.0,
          vol=None):
    """构造平稳日K；final bar 的振幅/量可单独放大以触发指标。"""
    bars = []
    day = datetime(2026, 10, 1)
    from datetime import timedelta
    for i in range(n):
        c = base_close + 0.01 * (i % 3)
        o = c - 0.01
        h = c + 0.05
        l = c - 0.05
        vol_row = vol if vol is not None else 100.0
        if i == n - 1 and last_ratio:
            h = c + 0.5 * last_ratio * 4  # 大幅抬高当日 TR
            l = c - 0.5 * last_ratio * 4
            vol_row = 100.0 * last_ratio
        bars.append((day.strftime("%Y-%m-%d"), o, c, h, l, vol_row))
        day += timedelta(days=1)
    return bars


def test_indicators_stable_and_spike():
    base = vw.indicators(_bars(60))
    assert base and base["atr_ratio"] < 2.5
    spike = vw.indicators(_bars(60, last_ratio=3))
    assert spike and spike["atr_ratio"] > 1.5
    assert "vol_ratio" in base


def test_update_history_rolling():
    st = {}
    for _ in range(150):
        vw.update_history(st, _snapshot())
    h = st["hist"]["BK0475"]["amount"]
    assert len(h) == 120 and all(a == 1_000_000.0 for a in h)


def test_prefilter_flags_amount_and_amp():
    st = {}
    for _ in range(40):
        vw.update_history(st, _snapshot())
    hot = {"code": "BK0475", "name": "银行", "price": 100.0, "pct": 2.0,
           "high": 103.0, "low": 100.5, "open": 100.1, "prev_close": 100.0,
           "volume": 2e5, "amount": 3_000_000.0, "amp": 2.5}
    calm = _snapshot()[0]
    rows = vw.prefilter(st, [hot, calm], vw.DEFAULT_CONFIG)
    assert [r[0] for r in rows] == ["BK0475"]
    assert rows and "振幅" in rows[0][2][0] or rows[0][2]


def test_check_action_wait_when_calm(monkeypatch, tmp_path):
    st = {"day": time.strftime("%Y-%m-%d"), "hist": {}}
    monkeypatch.setattr(vw, "in_session", lambda now=None: True)
    res = vw.check(st, {"verify_ttl_sec": 0}, do_fetch=False,
                   snapshot=_snapshot())
    assert res["action"] == "wait"


def test_check_activates_on_spike(monkeypatch):
    st = {"day": time.strftime("%Y-%m-%d"), "hist": {}}
    for _ in range(40):
        vw.update_history(st, _snapshot())
    monkeypatch.setattr(vw, "in_session", lambda now=None: True)
    monkeypatch.setattr(vw, "verify_daily",
                        lambda code, cfg, cache=None: (
                            {"atr_ratio": 2.5, "bw_z": 0.5,
                             "vol_ratio": 2.2}, 2))
    res = vw.check(st, {"verify_ttl_sec": 0}, do_fetch=False,
                   snapshot=_snapshot(amount=3_000_000.0, amp=2.5, pct=2.1))
    assert res["action"] == "activate"
    assert res["sector"]["name"] == "银行"
    assert not res.get("force")


def test_check_min_signals_not_met(monkeypatch):
    st = {"day": time.strftime("%Y-%m-%d"), "hist": {}}
    for _ in range(40):
        vw.update_history(st, _snapshot())
    monkeypatch.setattr(vw, "in_session", lambda now=None: True)
    monkeypatch.setattr(vw, "verify_daily",
                        lambda code, cfg, cache=None: (
                            {"atr_ratio": 1.5, "bw_z": 0.5,
                             "vol_ratio": 1.2}, 0))
    res = vw.check(st, {"verify_ttl_sec": 0}, do_fetch=False,
                   snapshot=_snapshot(amount=3_000_000.0, amp=2.5, pct=2.1))
    assert res["action"] == "wait"


def test_check_force_after_quiet(monkeypatch):
    st = {"day": time.strftime("%Y-%m-%d"), "hist": {},
          "session_started_at": time.time() - 7200,
          "last_ts": datetime.fromtimestamp(time.time() - 3700)
          .isoformat(timespec="seconds"),
          "activated": 1}
    monkeypatch.setattr(vw, "in_session", lambda now=None: True)
    res = vw.check(st, {"verify_ttl_sec": 0, "quiet_minutes": 60,
                        "min_cooldown_min": 1}, do_fetch=False,
                   snapshot=_snapshot())
    assert res["action"] == "activate" and res.get("force")


def test_check_cooldown_blocks(monkeypatch):
    st = {"day": time.strftime("%Y-%m-%d"), "hist": {},
          "session_started_at": time.time() - 3700,
          "last_ts": datetime.now().isoformat(timespec="seconds"),
          "activated": 1}
    monkeypatch.setattr(vw, "in_session", lambda now=None: True)
    monkeypatch.setattr(vw, "verify_daily",
                        lambda code, cfg, cache=None: ({}, 3))
    res = vw.check(st, {"verify_ttl_sec": 0, "min_cooldown_min": 15},
                   do_fetch=False,
                   snapshot=_snapshot(amount=5_000_000.0, amp=3.0, pct=3.0))
    assert res["action"] == "wait"


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "s.json")
    st = {"day": "2026-11-06", "hist": {"BK0475": {"name": "银行",
                                                  "amount": [1.0]}}}
    vw.save_state(st, p)
    got = vw.load_state(p)
    assert got["hist"]["BK0475"]["name"] == "银行"
