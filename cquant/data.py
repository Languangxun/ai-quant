from __future__ import annotations

import os
import sqlite3

import numpy as np
import pandas as pd

BAR_COLS = ["open", "high", "low", "close", "vol"]
_CODE_RE = r"^[A-Za-z0-9]+USDT$"


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"行情库不存在: {db_path}\n"
            "请修改 config/crypto.yaml 的 data.db，或在桌面 sm701 目录下准备 stock_cache.db")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=TRUE")
    return con


def load_crypto_bars(db_path: str, start: str = "2018-01-01",
                     end: str = "", min_bars: int = 120,
                     table: str = "crypto_daily") -> pd.DataFrame:
    """读取 USDT 永续日线（24/7），只保留 ASCII 合约代码。

    sm701 采集的 `vol` 为 USDT 成交额，因此流动性过滤直接用 vol。
    """
    con = connect(db_path)
    sql = (f"SELECT code, date, open, high, low, close, vol FROM {table} "
           "WHERE date >= ?")
    params: list = [start]
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY code, date"
    try:
        df = pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()
    if df.empty:
        raise RuntimeError("crypto_daily 为空，检查 data.start / table")

    df["code"] = df["code"].astype(str)
    df = df[df["code"].str.fullmatch(_CODE_RE)].copy()
    for c in BAR_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df[df[BAR_COLS].notna().all(axis=1)]
    df = df[(df["close"] > 0) & (df["open"] > 0)
            & (df["high"] > 0) & (df["low"] > 0) & (df["high"] >= df["low"])]
    n = df.groupby("code")["date"].transform("count")
    df = df[n >= min_bars].copy()
    df = df.sort_values(["code", "date"], kind="stable").reset_index(drop=True)
    df["code"] = pd.Categorical(df["code"], categories=sorted(df["code"].unique()))
    df["lim"] = np.float32(1.0)          # 加密无涨跌停
    df["hist_len"] = df.groupby("code", sort=False).cumcount() + 1
    df["turnover20"] = df.groupby("code", sort=False)["vol"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    df.attrs["n_codes"] = int(df["code"].nunique())
    df.attrs["n_dates"] = int(df["date"].nunique())
    return df


def price_pivots(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """date × code 的开高低收矩阵（模拟器逐日读取）。"""
    out = {}
    for col in ("open", "high", "low", "close"):
        out[col] = df.pivot(index="date", columns="code", values=col).sort_index()
        out[col].columns = out[col].columns.astype(str)
    return out


def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change()
