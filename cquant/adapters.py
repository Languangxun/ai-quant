from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pandas as pd

_cache: dict[str, object] = {}

ALPHA_FEATURES_FALLBACK = [
    "ret1", "ret5", "ret10", "ret20", "ret60", "ma5r", "ma10r", "ma20r",
    "ma60r", "ma5_20", "ma20_60", "vol20", "vol60", "volr", "rsi6", "rsi14",
    "macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d", "kdj_j", "bollpos",
    "hilo20", "atr_n", "amp", "gap", "cpos", "vr5", "vr20", "logvol",
    "updays20", "r_ret5", "r_ret20", "r_ret60", "r_ma20r", "r_rsi14",
    "r_vr20", "r_vol20", "r_logvol",
]


def _load(path: str | os.PathLike, name: str):
    path = str(path)
    key = f"{name}:{path}"
    if key in _cache:
        return _cache[key]
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 sm701/sm702 模块: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    _cache[key] = mod
    return mod


def alpha_feature_names(sm701_dir: str) -> list[str]:
    path = Path(sm701_dir) / "production" / "features_tech.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return list(json.load(f)["features"])
    return list(ALPHA_FEATURES_FALLBACK)


def sm701_tech_features(df: pd.DataFrame, sm701_dir: str,
                        features: list[str] | None = None) -> pd.DataFrame:
    """调用 sm701 的 `_compute_feats`，返回 40 维 tech 因子矩阵。"""
    mod = _load(Path(sm701_dir) / "lgbm_train_backtest.py", "cquant_sm701_tech")
    feats = features or alpha_feature_names(sm701_dir)
    raw = mod._compute_feats(df)
    X = pd.DataFrame({k: raw[k] for k in feats}, index=df.index)
    return X.astype("float32")


def sm702_risk_features(df: pd.DataFrame, sm702_dir: str) -> pd.DataFrame:
    """调用 sm702 `features.compute_features`，返回 120 维顶部因子矩阵。"""
    mod = _load(Path(sm702_dir) / "features.py", "cquant_sm702_feats")
    X = mod.compute_features(df)
    if len(X) != len(df):
        raise RuntimeError("sm702 特征行数与行情不一致")
    X.index = df.index
    return X.astype("float32")


def sm702_risk_labels(df: pd.DataFrame, sm702_dir: str, horizon: int = 10,
                      dd: float = 0.08, newhigh: float = 0.03,
                      max_gap: int = 5) -> pd.DataFrame:
    mod = _load(Path(sm702_dir) / "features.py", "cquant_sm702_feats")
    out = mod.compute_labels(df, horizon=horizon, dd_threshold=dd,
                             newhigh_threshold=newhigh, max_gap=max_gap)
    out.index = df.index
    return out
