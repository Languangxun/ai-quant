from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data": {
        "db": "/home/lan/桌面/sm701/stock_cache.db",
        "table": "crypto_daily",
        "start": "2018-01-01",
        "end": "",
        "min_bars": 120,
        "btc": "BTCUSDT",
        "exclude_codes": ["BTCUSDT"],
    },
    "vendors": {
        "sm701": "/home/lan/桌面/sm701",
        "sm702": "/home/lan/桌面/sm702",
    },
    "model": {
        "source": "walkforward",
        "test_years": [2022, 2023, 2024, 2025, 2026],
        "train_years": 4,
        "sample_start": "2018-06-01",
        "horizon": 10,
        "min_train": 15000,
        "threads": 8,
        "pretrained": {
            "sm701_alpha": "markets_output/crypto_ab_E13_h10.txt",
            "sm702_risk": "outputs_crypto/models/lgbm_{year}.txt",
            "alpha_valid_from": "2024-01-01",
        },
        "alpha": {},
        "risk": {},
    },
    "fusion": {
        "risk_penalty": 0.5,
        "risk_gate_prob": 0.45,
        "risk_gate_quantile": 0.85,
        "hysteresis": 6,
        "regime": {"enabled": True, "lookback": 40, "q_high": 0.80,
                   "scale_high": 0.7, "q_extreme": 0.95, "scale_extreme": 0.4},
    },
    "portfolio": {
        "hedge_mode": "none",
        "long_only": False,
        "n_long": 12,
        "n_short": 12,
        "book_fraction": 0.25,
        "leverage": 2.0,
        "max_leverage": 3.0,
        "vol_target": 0.35,
        "vol_upscale": False,
        "min_vol_scale": 0.25,
        "signal_smooth_days": 3,
        "rebalance_days": 10,
        "min_hist": 120,
        "min_turnover_usdt": 3_000_000,
        "btc_code": "BTCUSDT",
        "max_weight": 0.25,
        "fee_rate": 0.0005,
        "slippage": 0.0005,
        "funding_daily": 0.0001,
        "funding_mode": "net",
        "maintenance_rate": 0.01,
        "liq_fee": 0.005,
        "liq_cooldown_days": 5,
        "capital": 100_000.0,
    },
    "backtest": {
        "start": "2022-01-01",
        "end": "",
        "ablations": True,
        "leverage_sweep": [1.0, 1.5, 2.0, 3.0],
    },
    "output": {"dir": "outputs"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    out_dir = cfg["output"]["dir"]
    if not os.path.isabs(out_dir):
        cfg["output"]["dir"] = os.path.abspath(out_dir)
    return cfg


def project_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
