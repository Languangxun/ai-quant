from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 365.0


def perf(equity: pd.Series | np.ndarray, freq: float = TRADING_DAYS) -> dict:
    eq = pd.Series(equity).astype("float64").dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {}
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    n = len(rets)
    ann = float((1.0 + total) ** (freq / n) - 1.0)
    vol = float(rets.std(ddof=1) * np.sqrt(freq))
    sharpe = float(rets.mean() / (rets.std(ddof=1) + 1e-12) * np.sqrt(freq))
    downside = rets[rets < 0].std(ddof=1)
    sortino = float(rets.mean() / (downside + 1e-12) * np.sqrt(freq))
    curve = eq / eq.cummax()
    mdd = float((curve - 1.0).min())
    calmar = float(ann / (abs(mdd) + 1e-12))
    return {
        "total_return": total,
        "ann_return": ann,
        "ann_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "mdd": mdd,
        "calmar": calmar,
        "win_daily": float((rets > 0).mean()),
        "days": int(n),
        "final_equity": float(eq.iloc[-1]),
    }


def yearly(equity: pd.Series, dates: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"date": pd.to_datetime(dates).to_numpy(),
                       "equity": np.asarray(equity, dtype="float64")})
    df["year"] = df["date"].dt.year
    rows = []
    prev = df["equity"].iloc[0]
    for year, g in df.groupby("year", sort=True):
        start = prev
        end = float(g["equity"].iloc[-1])
        ret = end / start - 1.0 if start > 0 else np.nan
        sub = g["equity"].to_numpy()
        rets = sub[1:] / sub[:-1] - 1.0 if len(sub) > 1 else np.array([])
        mdd = float((sub / np.maximum.accumulate(sub) - 1.0).min()) if len(sub) else np.nan
        rows.append({"year": int(year), "return": ret,
                     "sharpe": float(rets.mean() / (rets.std(ddof=1) + 1e-12)
                                     * np.sqrt(TRADING_DAYS)) if len(rets) > 2 else np.nan,
                     "mdd": mdd, "days": int(len(sub))})
        prev = end
    return pd.DataFrame(rows)


def summary_row(name: str, daily: pd.DataFrame, stats: dict) -> dict:
    m = perf(daily["equity"])
    row = {"variant": name, **m}
    row.update({
        "avg_gross": float(daily["gross"].div(daily["equity"]).replace(
            [np.inf, -np.inf], np.nan).fillna(0).mean()),
        "avg_net": float(daily["net"].div(daily["equity"]).replace(
            [np.inf, -np.inf], np.nan).fillna(0).mean()),
        "avg_positions": float(daily["n_pos"].mean()),
        "avg_turnover": stats["avg_turnover"],
        "liquidations": stats["liquidation_count"],
        "fee_paid": stats["fee_paid"],
        "funding_paid": stats["funding_paid"],
    })
    return row
