from __future__ import annotations

import numpy as np
import pandas as pd


def build_beta(df: pd.DataFrame, btc: str = "BTCUSDT", win: int = 90,
               min_win: int = 60) -> pd.Series:
    """每币对 BTC 的 90 日滚动 beta（与 sm701 train_crypto_ab 同口径）。"""
    ret1 = df.groupby("code", sort=False)["close"].pct_change()
    tmp = pd.DataFrame({
        "code": df["code"].astype(str).to_numpy(),
        "date": df["date"].to_numpy(),
        "r": ret1.to_numpy(),
    })
    piv = tmp.pivot_table(index="date", columns="code", values="r")
    if btc not in piv.columns:
        raise RuntimeError(f"行情缺少基准币 {btc}")
    y = piv[btc]
    x = piv.drop(columns=[btc])
    rm_xy = x.mul(y, axis=0).rolling(win, min_win).mean()
    rm_x = x.rolling(win, min_win).mean()
    rm_y = y.rolling(win, min_win).mean()
    var_y = y.rolling(win, min_win).var()
    beta = (rm_xy - rm_x.mul(rm_y, axis=0)).div(var_y, axis=0)
    beta[btc] = 1.0
    stacked = beta.stack()
    idx = pd.MultiIndex.from_arrays(
        [df["date"].to_numpy(), df["code"].astype(str).to_numpy()])
    out = stacked.reindex(idx)
    return pd.Series(out.to_numpy(dtype=np.float32), index=df.index, name="beta")


def build_alpha_labels(df: pd.DataFrame, beta: pd.Series, horizon: int = 10,
                       btc: str = "BTCUSDT") -> pd.DataFrame:
    """sm701 路线标签：未来 h 日相对 BTC 的 beta 中性残差收益，按日截面中位数二分类。"""
    gb = df.groupby("code", sort=False)
    open1 = gb["open"].shift(-1)
    close_h = gb["close"].shift(-horizon)
    R = close_h / open1 - 1.0
    is_btc = df["code"].astype(str).eq(btc).to_numpy()
    btc_map = dict(zip(df.loc[is_btc, "date"], R[is_btc]))
    Rb = df["date"].map(btc_map).astype("float64")
    R_ex = R - beta * Rb
    ok = (R.notna() & Rb.notna() & beta.notna()).to_numpy()
    med_src = R_ex.mask(is_btc)          # BTC 自身 R_ex=0，不参与中位数
    med = med_src.groupby(df["date"]).transform("median")
    y = (R_ex > med).astype("int8").where(ok, 0)
    n_day = R.groupby(df["date"]).transform("count")
    out = pd.DataFrame({
        "R": R.astype("float32"),
        "R_ex": R_ex.astype("float32"),
        "y": y.astype("int8"),
        "ok": ok,
        "n_day": n_day.astype("int32"),
    }, index=df.index)
    return out


def alpha_time_weights(dates: pd.Series, n_day: np.ndarray, ref_date: str,
                       halflife_years: float = 2.0) -> np.ndarray:
    """截面数（上限 50）+ 时间衰减，和 sm701 E3 一致。"""
    age = (pd.Timestamp(ref_date) - pd.to_datetime(dates)).dt.days.to_numpy() / 365.0
    w = np.clip(n_day.astype("float64"), None, 50.0) / 50.0
    w *= 0.5 ** (np.clip(age, 0.0, None) / halflife_years)
    return w.astype("float32")


def time_decay_weights(dates: pd.Series, ref_date: str,
                       halflife_years: float = 3.0) -> np.ndarray:
    age = (pd.Timestamp(ref_date) - pd.to_datetime(dates)).dt.days.to_numpy() / 365.0
    return (0.5 ** (np.clip(age, 0.0, None) / halflife_years)).astype("float32")
