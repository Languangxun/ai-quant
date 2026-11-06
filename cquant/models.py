from __future__ import annotations

import os
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .features import alpha_time_weights, time_decay_weights


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _first_ge(dates: np.ndarray, target: str) -> int:
    i = int(np.searchsorted(dates, target, side="left"))
    return min(i, len(dates) - 1)


def _last_le(dates: np.ndarray, target: str) -> int:
    i = int(np.searchsorted(dates, target, side="right")) - 1
    return max(i, 0)


def fold_ranges(dates: np.ndarray, year: int, train_years: int,
                sample_start: str, horizon: int) -> dict | None:
    te0 = _first_ge(dates, f"{year}-01-01")
    te1 = _last_le(dates, f"{year}-12-31")
    va0 = _first_ge(dates, f"{year - 1}-01-01")
    va1 = te0 - (horizon + 2)
    tr0 = _first_ge(dates, max(sample_start, f"{year - train_years}-01-01"))
    tr1 = va0 - (horizon + 2)
    if str(dates[te0])[:4] != str(year):
        return None
    if va1 < va0 + 40 or tr1 < tr0 + 120:
        return None
    return {"year": year, "tr0": tr0, "tr1": tr1, "va0": va0, "va1": va1,
            "te0": te0, "te1": te1}


def _fit(X, y, w, Xv, yv, params: dict, threads: int) -> tuple[lgb.LGBMClassifier, int]:
    es = int(params.get("early_stopping", 80))
    model = lgb.LGBMClassifier(
        objective="binary", metric="auc",
        learning_rate=float(params["learning_rate"]),
        num_leaves=int(params["num_leaves"]),
        min_data_in_leaf=int(params["min_data_in_leaf"]),
        feature_fraction=float(params["feature_fraction"]),
        bagging_fraction=float(params["bagging_fraction"]),
        bagging_freq=int(params["bagging_freq"]),
        lambda_l2=float(params["lambda_l2"]),
        n_estimators=int(params["n_estimators"]),
        max_bin=127, verbosity=-1, random_state=42, n_jobs=threads)
    model.fit(X, y, sample_weight=w, eval_set=[(Xv, yv)],
              callbacks=[lgb.early_stopping(es, verbose=False),
                         lgb.log_evaluation(0)])
    best = int(model.best_iteration_ or params["n_estimators"])
    return model, best


def _predict(model, X) -> np.ndarray:
    return np.asarray(model.predict_proba(X)[:, 1], dtype="float32")


def _alpha_ic_by_date(sub: pd.DataFrame, pred_col: str = "alpha",
                      ret_col: str = "R_ex") -> float:
    def _c(g: pd.DataFrame) -> float:
        if len(g) < 8 or g[pred_col].nunique() < 3 or g[ret_col].nunique() < 3:
            return np.nan
        return float(g[pred_col].corr(g[ret_col], method="spearman"))
    ic = sub.groupby("date").apply(_c)
    ic = ic.dropna()
    return float(ic.mean()) if len(ic) else float("nan")


def walkforward(ctx: dict, cfg: dict, years: list[int]) -> tuple[pd.DataFrame, list[dict]]:
    df = ctx["df"]
    Xa, Xr = ctx["Xa"], ctx["Xr"]
    al, rl = ctx["al"], ctx["rl"]
    m = cfg["model"]
    horizon = int(m["horizon"])
    out_dir = Path(cfg["output"]["dir"]) / "models"
    out_dir.mkdir(parents=True, exist_ok=True)

    dates = np.array(sorted(df["date"].unique()))
    dpos = np.searchsorted(dates, df["date"].to_numpy())
    hist = df["hist_len"].to_numpy()
    code_str = df["code"].astype(str).to_numpy()
    excl = set(cfg["data"].get("exclude_codes") or [])
    keep = (~np.isin(code_str, list(excl))) & (hist >= 60)

    a_ok = al["ok"].to_numpy().astype(bool) & keep
    r_ok = rl["label_valid"].to_numpy().astype(bool) & (hist >= 120)
    a_y = al["y"].to_numpy()
    r_y = rl["label_top"].fillna(0.0).to_numpy()
    n_day = al["n_day"].to_numpy()

    preds: list[pd.DataFrame] = []
    diag: list[dict] = []
    threads = int(m.get("threads") or os.cpu_count() or 4)

    for year in years:
        rng = fold_ranges(dates, int(year), int(m["train_years"]),
                          str(m["sample_start"]), horizon)
        if rng is None:
            log(f"[{year}] 训练窗口不足，跳过")
            continue
        tr = (dpos >= rng["tr0"]) & (dpos <= rng["tr1"])
        va = (dpos >= rng["va0"]) & (dpos <= rng["va1"])
        tr_a, va_a = tr & a_ok, va & a_ok
        tr_r, va_r = tr & r_ok, va & r_ok
        if tr_a.sum() < int(m["min_train"]) or va_a.sum() < 2000:
            log(f"[{year}] alpha 样本不足 ({tr_a.sum()}/{va_a.sum()})，跳过")
            continue

        ref_tr = str(dates[rng["tr1"]])
        w_a = alpha_time_weights(df.loc[tr_a, "date"], n_day[tr_a], ref_tr,
                                 halflife_years=2.0)
        model_a, best_a = _fit(Xa.loc[tr_a], a_y[tr_a], w_a,
                               Xa.loc[va_a], a_y[va_a], m["alpha"], threads)
        model_a.booster_.save_model(str(out_dir / f"alpha_{year}.txt"))

        if tr_r.sum() >= 3000 and va_r.sum() >= 500:
            w_r = time_decay_weights(df.loc[tr_r, "date"], ref_tr,
                                     float(m["risk"]["decay_halflife_years"]))
            model_r, best_r = _fit(Xr.loc[tr_r], r_y[tr_r], w_r,
                                   Xr.loc[va_r], r_y[va_r], m["risk"], threads)
            model_r.booster_.save_model(str(out_dir / f"risk_{year}.txt"))
        else:
            model_r, best_r = None, 0

        te = (dpos >= rng["te0"]) & (dpos <= rng["te1"]) & keep
        te_idx = np.flatnonzero(te)
        pa = _predict(model_a, Xa.iloc[te_idx])
        pr = np.full(len(te_idx), np.nan, dtype="float32")
        if model_r is not None:
            pr = _predict(model_r, Xr.iloc[te_idx])
        part = pd.DataFrame({
            "date": df["date"].to_numpy()[te_idx],
            "code": code_str[te_idx],
            "alpha": pa,
            "risk": pr,
        })
        preds.append(part)

        sub = part.copy()
        sub["R_ex"] = al["R_ex"].to_numpy()[te_idx]
        sub["top"] = rl["label_top"].to_numpy()[te_idx]
        ic = _alpha_ic_by_date(sub.dropna(subset=["R_ex"]))
        ok_top = np.isfinite(sub["top"].to_numpy())
        auc = (roc_auc_score(sub["top"].to_numpy()[ok_top].astype(int),
                             sub["risk"].to_numpy()[ok_top])
               if ok_top.sum() > 100 and len(np.unique(sub["top"].to_numpy()[ok_top])) > 1
               else float("nan"))
        row = {"year": int(year), "source": "walkforward",
               "train_start": str(dates[rng["tr0"]]),
               "train_end": str(dates[rng["tr1"]]),
               "valid_start": str(dates[rng["va0"]]),
               "valid_end": str(dates[rng["va1"]]),
               "test_start": str(dates[rng["te0"]]),
               "test_end": str(dates[rng["te1"]]),
               "n_train_alpha": int(tr_a.sum()), "n_test": int(len(te_idx)),
               "best_iter_alpha": best_a, "best_iter_risk": best_r,
               "alpha_ic": round(ic, 4),
               "risk_auc": round(float(auc), 4)}
        diag.append(row)
        log(f"[{year}] alpha iter={best_a} IC={ic:+.4f} | risk iter={best_r} "
            f"AUC={auc:.4f} | test={len(te_idx):,} 行")

    if not preds:
        raise RuntimeError("walkforward 没有产出任何预测，检查 test_years / 数据量")
    return pd.concat(preds, ignore_index=True), diag


def _load_booster(path: Path, X: pd.DataFrame) -> np.ndarray:
    bst = lgb.Booster(model_file=str(path))
    names = bst.feature_name()
    return np.asarray(bst.predict(X[names]), dtype="float32")


def pretrained(ctx: dict, cfg: dict, years: list[int]) -> tuple[pd.DataFrame, list[dict]]:
    """直接调用 sm701 / sm702 已训练模型（sm701 alpha 自 2024-01 起样本外）。"""
    df = ctx["df"]
    Xa, Xr = ctx["Xa"], ctx["Xr"]
    al, rl = ctx["al"], ctx["rl"]
    p = cfg["model"]["pretrained"]
    sm701 = Path(cfg["vendors"]["sm701"])
    sm702 = Path(cfg["vendors"]["sm702"])
    alpha_path = sm701 / p["sm701_alpha"]
    if not alpha_path.exists():
        raise FileNotFoundError(f"缺少 sm701 模型: {alpha_path}")

    dates = np.array(sorted(df["date"].unique()))
    dpos = np.searchsorted(dates, df["date"].to_numpy())
    hist = df["hist_len"].to_numpy()
    code_str = df["code"].astype(str).to_numpy()
    excl = set(cfg["data"].get("exclude_codes") or [])
    keep = (~np.isin(code_str, list(excl))) & (hist >= 60)

    valid_from = str(p.get("alpha_valid_from") or "2024-01-01")
    mask_a = keep & (df["date"].to_numpy() >= valid_from)
    idx_a = np.flatnonzero(mask_a)
    pa = _load_booster(alpha_path, Xa.iloc[idx_a])
    parts = [pd.DataFrame({"date": df["date"].to_numpy()[idx_a],
                           "code": code_str[idx_a], "alpha": pa})]

    diag: list[dict] = []
    risk_parts = []
    for year in years:
        rpath = sm702 / str(p["sm702_risk"]).format(year=int(year))
        m = keep & (df["date"].to_numpy() >= f"{int(year)}-01-01") \
            & (df["date"].to_numpy() <= f"{int(year)}-12-31")
        idx = np.flatnonzero(m)
        if len(idx) == 0:
            continue
        if not rpath.exists():
            log(f"[{year}] 缺少 sm702 模型 {rpath}，该年 risk 置空")
            pr = np.full(len(idx), np.nan, dtype="float32")
        else:
            pr = _load_booster(rpath, Xr.iloc[idx])
        risk_parts.append(pd.DataFrame({"date": df["date"].to_numpy()[idx],
                                        "code": code_str[idx], "risk": pr}))
        sub = pd.DataFrame({"top": rl["label_top"].to_numpy()[idx],
                            "risk": pr})
        ok = np.isfinite(sub["top"].to_numpy()) & np.isfinite(pr)
        auc = (roc_auc_score(sub["top"].to_numpy()[ok].astype(int),
                             pr[ok]) if ok.sum() > 100
               and len(np.unique(sub["top"].to_numpy()[ok])) > 1 else np.nan)
        ic_sub = pd.DataFrame({"date": df["date"].to_numpy()[idx],
                               "alpha": np.nan, "R_ex": al["R_ex"].to_numpy()[idx]})
        diag.append({"year": int(year), "source": "pretrained",
                     "test_start": str(dates[_first_ge(dates, f'{year}-01-01')]),
                     "test_end": str(dates[_last_le(dates, f'{year}-12-31')]),
                     "n_test": int(len(idx)), "risk_auc": round(float(auc), 4),
                     "alpha_ic": np.nan})

    risk = (pd.concat(risk_parts, ignore_index=True) if risk_parts
            else pd.DataFrame(columns=["date", "code", "risk"]))
    preds = parts[0].merge(risk, on=["date", "code"], how="left")
    return preds, diag


def predict_date(cfg: dict, ctx: dict, date: str,
                 source: str = "auto") -> pd.DataFrame:
    """对指定日期做双模型打分（predict 命令用）。"""
    df = ctx["df"]
    Xa, Xr = ctx["Xa"], ctx["Xr"]
    idx = np.flatnonzero(df["date"].to_numpy() == date)
    if len(idx) == 0:
        raise RuntimeError(f"数据里没有 {date}")
    out = pd.DataFrame({"date": date, "code": df["code"].astype(str).to_numpy()[idx]})

    models_dir = Path(cfg["output"]["dir"]) / "models"
    years = sorted(cfg["model"]["test_years"])
    year = years[-1]
    alpha_path = models_dir / f"alpha_{year}.txt"
    risk_path = models_dir / f"risk_{year}.txt"
    if (source in ("auto", "walkforward")
            and alpha_path.exists() and risk_path.exists()):
        out["alpha"] = _load_booster(alpha_path, Xa.iloc[idx])
        out["risk"] = _load_booster(risk_path, Xr.iloc[idx])
        out.attrs["model_source"] = f"walkforward:{year}"
        return out

    p = cfg["model"]["pretrained"]
    alpha_path = Path(cfg["vendors"]["sm701"]) / p["sm701_alpha"]
    rpath = Path(cfg["vendors"]["sm702"]) / str(p["sm702_risk"]).format(year=year)
    if not alpha_path.exists() or not rpath.exists():
        raise FileNotFoundError("没有可用的已训练模型，先运行 backtest")
    out["alpha"] = _load_booster(alpha_path, Xa.iloc[idx])
    out["risk"] = _load_booster(rpath, Xr.iloc[idx])
    out.attrs["model_source"] = f"pretrained:{year}"
    return out
