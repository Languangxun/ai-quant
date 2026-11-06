from __future__ import annotations

import pytest

from cquant.fusion import book_vol_scale, build_book, regime_scale


def _snap(n=20):
    rows = []
    for i in range(n):
        rows.append({
            "date": "2024-01-01",
            "code": f"C{i:02d}USDT",
            "alpha": 0.9 - 0.02 * i,
            "risk": 0.10 + 0.01 * i,
            "close": 10.0 + i,
            "hist_len": 500,
            "turnover20": 1e8,
        })
    return __import__("pandas").DataFrame(rows)


PORT = {
    "hedge_mode": "cross_section", "n_long": 5, "n_short": 5,
    "book_fraction": 0.5, "min_hist": 120, "min_turnover_usdt": 1e6,
    "max_weight": 0.5, "btc_code": "BTCUSDT",
}
FUS = {"risk_penalty": 0.5, "risk_gate_prob": 0.45,
       "risk_gate_quantile": 0.85, "hysteresis": 0,
       "regime": {"enabled": True, "lookback": 40, "q_high": 0.80,
                  "scale_high": 0.7, "q_extreme": 0.95, "scale_extreme": 0.4}}


def test_weights_are_long_short_and_gross():
    book = build_book(_snap(), FUS, PORT, leverage=2.0)
    assert book.weights
    gross = sum(abs(v) for v in book.weights.values())
    net = sum(book.weights.values())
    assert gross == pytest.approx(2.0, abs=1e-6)
    assert net == pytest.approx(0.0, abs=1e-6)
    assert sum(v > 0 for v in book.weights.values()) == 5
    assert sum(v < 0 for v in book.weights.values()) == 5


def test_risk_gate_blocks_top_risk_name():
    snap = _snap()
    # 最高 alpha 的币同时拥有最高风险，硬门槛应把它从多头剔除
    snap.loc[snap["code"] == "C00USDT", "risk"] = 0.95
    book = build_book(snap, FUS, PORT, leverage=2.0)
    assert book.weights.get("C00USDT", 0.0) <= 0.0


def test_risk_penalty_changes_ranking():
    snap = _snap()
    # 让中段币的风险最低、高 alpha 币风险偏高，惩罚项会改变多空名单
    snap["risk"] = [0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.12, 0.14,
                    0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.32,
                    0.34, 0.36]
    neutral = build_book(snap, {**FUS, "risk_penalty": 0.0,
                                "risk_gate_prob": 1.01,
                                "risk_gate_quantile": 0.0}, PORT, 2.0)
    fused = build_book(snap, {**FUS, "risk_gate_quantile": 0.0}, PORT, 2.0)
    assert neutral.weights != fused.weights


def test_long_only_allocation():
    book = build_book(_snap(), FUS, {**PORT, "hedge_mode": "none"},
                      leverage=1.5)
    assert all(v > 0 for v in book.weights.values())
    assert sum(book.weights.values()) == pytest.approx(1.5, abs=1e-6)


def test_btc_beta_hedge():
    snap = _snap()
    snap["beta"] = 1.2
    book = build_book(snap, FUS, {**PORT, "hedge_mode": "btc"}, leverage=2.0)
    btc_w = book.weights["BTCUSDT"]
    long_sum = sum(v for c, v in book.weights.items() if c != "BTCUSDT")
    assert btc_w < 0
    assert btc_w == pytest.approx(-1.2 * long_sum, abs=1e-9)
    assert sum(abs(v) for v in book.weights.values()) == pytest.approx(
        2.0, abs=1e-6)
    # beta 对冲按 beta 名义做空，净敞口 = 多头*(1-beta)，不是美元中性
    assert sum(book.weights.values()) == pytest.approx(long_sum * (1 - 1.2),
                                                      abs=1e-9)


def test_regime_scale():
    cfg = FUS["regime"]
    assert regime_scale(0.30, cfg) == 1.0
    assert regime_scale(0.85, cfg) == pytest.approx(0.7)
    assert regime_scale(0.98, cfg) == pytest.approx(0.4)
    off = {**cfg, "enabled": False}
    assert regime_scale(0.98, off) == 1.0


def test_hysteresis_keeps_old_names():
    snap = _snap()
    first = build_book(snap, FUS, PORT, 2.0)
    olds = {c for c, w in first.weights.items() if w > 0}
    # 新的一天 alpha 完全反转：无滞回会全换，有滞回会保留部分老仓位
    snap2 = snap.copy()
    snap2["alpha"] = -snap2["alpha"]
    kept = build_book(snap2, {**FUS, "hysteresis": 6}, PORT, 2.0,
                      prev_long=olds)
    new = build_book(snap2, {**FUS, "hysteresis": 6}, PORT, 2.0)
    overlap_kept = len(olds & {c for c, w in kept.weights.items() if w > 0})
    overlap_new = len(olds & {c for c, w in new.weights.items() if w > 0})
    assert overlap_kept > overlap_new


def test_vol_scale_bounds():
    import numpy as np
    import pandas as pd
    dates = pd.date_range("2024-01-01", periods=40).strftime("%Y-%m-%d")
    ret = pd.DataFrame({"AUSDT": np.full(40, 0.01), "BUSDT": np.full(40, 0.01)})
    ret.index = dates
    w = {"AUSDT": 0.5, "BUSDT": -0.5}
    s = book_vol_scale(w, ret, dates[-1], target_vol=0.3, min_scale=0.2,
                       max_scale=2.0)
    assert 0.2 <= s <= 1.0
    s2 = book_vol_scale(w, ret, dates[-1], target_vol=0.3, min_scale=0.2,
                        max_scale=2.0, allow_upscale=True)
    assert 0.2 <= s2 <= 2.0
