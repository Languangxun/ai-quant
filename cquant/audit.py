from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import adapters, data as data_mod
from .features import build_alpha_labels, build_beta


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def prepare(cfg: dict) -> dict:
    d = cfg["data"]
    df = data_mod.load_crypto_bars(d["db"], d["start"], d["end"],
                                   int(d["min_bars"]), d["table"])
    log(f"行情: {df.attrs['n_codes']} 币 / {df.attrs['n_dates']} 日 / {len(df):,} 行")
    log("计算 sm701 40 维 tech 因子 ...")
    Xa = adapters.sm701_tech_features(df, cfg["vendors"]["sm701"])
    log("计算 sm702 120 维顶部因子 ...")
    Xr = adapters.sm702_risk_features(df, cfg["vendors"]["sm702"])
    log("计算 beta 与 alpha 标签 ...")
    beta = build_beta(df, cfg["data"]["btc"])
    al = build_alpha_labels(df, beta, int(cfg["model"]["horizon"]), cfg["data"]["btc"])
    log("计算 sm702 顶部标签 ...")
    rk = cfg["model"]["risk"]
    rl = adapters.sm702_risk_labels(df, cfg["vendors"]["sm702"],
                                    int(cfg["model"]["horizon"]),
                                    float(rk["dd_threshold"]),
                                    float(rk["newhigh_threshold"]))
    return {"df": df, "Xa": Xa, "Xr": Xr, "beta": beta, "al": al, "rl": rl}


def audit(cfg: dict) -> pd.DataFrame:
    """三项未来函数审计：特征不变性 / 标签尾部无效 / 折区间隔离。"""
    import lightgbm as lgb  # noqa: F401

    d = cfg["data"]
    df = data_mod.load_crypto_bars(d["db"], d["start"], d["end"],
                                   int(d["min_bars"]), d["table"])
    dates = np.array(sorted(df["date"].unique()))
    cut = str(dates[len(dates) * 3 // 4])
    codes = (df.groupby("code")["date"].count().sort_values(ascending=False)
             .head(10).index.astype(str).tolist())
    keep = df["code"].astype(str).isin(codes)
    full = df[keep].copy()
    full["code"] = full["code"].astype(str)
    trunc = full[full["date"] <= cut].copy()
    full["code"] = pd.Categorical(full["code"], categories=sorted(codes))
    trunc["code"] = pd.Categorical(trunc["code"], categories=sorted(codes))

    Xa_f = adapters.sm701_tech_features(full, cfg["vendors"]["sm701"])
    Xa_t = adapters.sm701_tech_features(trunc, cfg["vendors"]["sm701"])
    Xr_f = adapters.sm702_risk_features(full, cfg["vendors"]["sm702"])
    Xr_t = adapters.sm702_risk_features(trunc, cfg["vendors"]["sm702"])

    def _cmp(a: pd.DataFrame, b: pd.DataFrame, name: str) -> dict:
        key = list(zip(full["date"], full["code"].astype(str)))
        a = a.copy(); a["k"] = key
        keyt = list(zip(trunc["date"], trunc["code"].astype(str)))
        b = b.copy(); b["k"] = keyt
        m = a.merge(b, on="k", suffixes=("_f", "_t"))
        diffs = []
        for col in a.columns:
            if col.endswith("_f") or col == "k":
                continue
            base = col[:-2] if col.endswith("_t") else col
            cf, ct = f"{base}_f", f"{base}_t"
            if cf not in m or ct not in m:
                continue
            x, y = m[cf].to_numpy(), m[ct].to_numpy()
            both = np.isfinite(x) & np.isfinite(y)
            if both.any():
                diffs.append(float(np.nanmax(np.abs(x[both] - y[both]))))
            if (np.isfinite(x) != np.isfinite(y)).any():
                diffs.append(np.inf)
        return {"check": name, "max_abs_diff": max(diffs) if diffs else np.nan,
                "passed": bool(max(diffs) < 1e-3) if diffs else False}

    rl = adapters.sm702_risk_labels(df, cfg["vendors"]["sm702"])
    tail = rl.groupby(df["code"], sort=False).tail(int(cfg["model"]["horizon"]))
    label_tail_ok = bool((~tail["label_valid"].astype(bool)).all())

    from .models import fold_ranges
    split_ok = True
    for year in cfg["model"]["test_years"]:
        r = fold_ranges(dates, int(year), int(cfg["model"]["train_years"]),
                        str(cfg["model"]["sample_start"]),
                        int(cfg["model"]["horizon"]))
        if r is None:
            continue
        split_ok &= (r["tr1"] < r["va0"]) and (r["va1"] < r["te0"])

    rows = [_cmp(Xa_f, Xa_t, "sm701 特征不变性"),
            _cmp(Xr_f, Xr_t, "sm702 特征不变性"),
            {"check": "标签尾部无效", "max_abs_diff": 0.0, "passed": label_tail_ok},
            {"check": "折区间隔离", "max_abs_diff": 0.0, "passed": bool(split_ok)}]
    out = pd.DataFrame(rows)
    log(out.to_string(index=False))
    return out
