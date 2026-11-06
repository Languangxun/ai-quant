from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Book:
    weights: dict[str, float]
    meta: dict = field(default_factory=dict)


@dataclass
class SimConfig:
    capital: float = 100_000.0
    fee_rate: float = 0.0005
    slippage: float = 0.0005
    funding_daily: float = 0.0001
    maintenance_rate: float = 0.01
    liq_fee: float = 0.005
    liq_cooldown_days: int = 5
    max_leverage: float = 3.0
    funding_mode: str = "net"   # net=多空对冲后按净敞口；gross=保守全收


def _get(row: pd.Series, code: str) -> float:
    try:
        v = row.get(code, np.nan)
    except Exception:
        return float("nan")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return v if np.isfinite(v) and v > 0 else float("nan")


def simulate(px: dict[str, pd.DataFrame], books: dict[str, Book],
             dates: list[str], cfg: SimConfig) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """按日推进的杠杆账本模拟。

    books 的 key 是信号日（收盘生成），在下一个交易日开盘成交。
    逐日：开盘调仓 -> 盘中强平检查 -> 收盘盯市 -> 扣资金费。
    """
    open_df, high_df, low_df, close_df = (px["open"], px["high"],
                                          px["low"], px["close"])
    idx_of = {d: i for i, d in enumerate(dates)}
    exec_map: dict[int, Book] = {}
    for sig_date, book in books.items():
        i = idx_of.get(sig_date)
        if i is None:
            continue
        if i + 1 < len(dates):
            exec_map[i + 1] = book

    qty: dict[str, float] = {}
    cash = float(cfg.capital)
    last_close: dict[str, float] = {}
    ref: dict[str, float] = {}
    equity_prev = float(cfg.capital)
    cooldown_until = -1
    rows: list[dict] = []
    trades: list[dict] = []
    n_liq = 0
    fee_paid = 0.0
    funding_paid = 0.0
    turnover_sum = 0.0

    for i, d in enumerate(dates):
        row_o, row_h = open_df.loc[d], high_df.loc[d]
        row_l, row_c = low_df.loc[d], close_df.loc[d]
        turnover = 0.0
        cost = 0.0
        liquidated = False

        # 1) 开盘：先做杠杆硬约束，再按信号调仓
        if equity_prev > 0:
            eq_open = cash
            for c, q in qty.items():
                p = _get(row_o, c)
                if not np.isfinite(p):
                    p = last_close.get(c, np.nan)
                if np.isfinite(p):
                    eq_open += q * p
            traded = 0.0

            def _trade_to(weights: dict[str, float], eq_ref: float) -> float:
                nonlocal cash, cost
                moved = 0.0
                for c in set(qty) | set(weights):
                    p = _get(row_o, c)
                    if not np.isfinite(p):
                        continue
                    tgt = float(weights.get(c, 0.0)) * eq_ref / p
                    cur = qty.get(c, 0.0)
                    delta = tgt - cur
                    if abs(delta) * p < 1e-6:
                        continue
                    notional = delta * p
                    cst = abs(notional) * (cfg.fee_rate + cfg.slippage)
                    cash -= notional + cst
                    cost += cst
                    moved += abs(notional)
                    if abs(tgt) < 1e-12:
                        qty.pop(c, None)
                    else:
                        qty[c] = tgt
                    ref[c] = p
                    trades.append({"date": d, "code": c,
                                   "side": "BUY" if delta > 0 else "SELL",
                                   "price": p, "notional": notional,
                                   "weight": float(weights.get(c, 0.0))})
                return moved

            if eq_open > 0:
                gross_open = 0.0
                for c, q in qty.items():
                    p = _get(row_o, c)
                    if not np.isfinite(p):
                        p = last_close.get(c, np.nan)
                    if np.isfinite(p):
                        gross_open += abs(q * p)
                if (gross_open > cfg.max_leverage * eq_open * 1.001
                        and gross_open > 0):
                    k = cfg.max_leverage * eq_open / gross_open
                    cur_w = {}
                    for c, q in qty.items():
                        p = _get(row_o, c)
                        if np.isfinite(p):
                            cur_w[c] = q * p / eq_open
                    traded += _trade_to({c: w * k for c, w in cur_w.items()},
                                        eq_open - cost)
                if i in exec_map and i > cooldown_until:
                    traded += _trade_to(exec_map[i].weights, eq_open - cost)
                if traded > 0:
                    turnover = traded / eq_open
                    fee_paid += cost
                for c in list(qty):
                    if c not in ref:
                        ref[c] = last_close.get(c, np.nan)

        # 2) 盘中强平检查（以昨收/开盘为基准，多仓取 low、空仓取 high）
        gross_ref = 0.0
        worst_pnl = 0.0
        for c, q in qty.items():
            base = ref.get(c, last_close.get(c, np.nan))
            if not np.isfinite(base):
                continue
            if q > 0:
                p_worst = _get(row_l, c)
            else:
                p_worst = _get(row_h, c)
            if not np.isfinite(p_worst):
                p_worst = last_close.get(c, base)
            worst_pnl += q * (p_worst - base)
            gross_ref += abs(q * base)
        if gross_ref > 0:
            eq_worst = equity_prev + worst_pnl
            if eq_worst <= cfg.maintenance_rate * gross_ref:
                eq_liq = max(0.0, eq_worst - cfg.liq_fee * gross_ref)
                cash = eq_liq
                qty.clear()
                ref.clear()
                cooldown_until = i + int(cfg.liq_cooldown_days)
                equity_prev = eq_liq
                liquidated = True
                n_liq += 1

        # 3) 收盘盯市 + 资金费
        gross = 0.0
        net = 0.0
        for c, q in qty.items():
            p = _get(row_c, c)
            if np.isfinite(p):
                last_close[c] = p
            else:
                p = last_close.get(c, np.nan)
            if np.isfinite(p):
                gross += abs(q * p)
                net += q * p
        base = abs(net) if cfg.funding_mode == "net" else gross
        funding = cfg.funding_daily * base
        cash -= funding
        funding_paid += funding
        equity = cash + net

        if not liquidated:
            equity_prev = equity
        for c in qty:
            p = last_close.get(c)
            if np.isfinite(p):
                ref[c] = p

        # 把每日元数据拉进来：books 是 keyed 为日期的 dict，
        # 但 simulate 的 dates 列表可能与 books.key 不完全一致（因为有 cooldown/liq）。
        # 用当天 d 在 books 里的 meta 提取 vol_60h / target_vol_adj（有则用，无则 NaN）。
        bm = books.get(d, {})
        vol_60h_val = bm.meta.get("vol_60h", np.nan) if hasattr(bm, "meta") else np.nan
        tv_adj_val = bm.meta.get("target_vol_adj", np.nan) if hasattr(bm, "meta") else np.nan

        turnover_sum += turnover
        rows.append({
            "date": d,
            "equity": float(equity),
            "gross": float(gross),
            "net": float(net),
            "leverage": float(gross / equity) if equity > 1e-9 else 0.0,
            "n_pos": int(len(qty)),
            "turnover": float(turnover),
            "cost": float(cost),
            "funding": float(funding),
            "liquidated": int(liquidated),
            "vol_60h": float(vol_60h_val) if np.isfinite(vol_60h_val) else np.nan,
            "target_vol_adj": float(tv_adj_val) if np.isfinite(tv_adj_val) else np.nan,
        })
        if equity <= 1e-9:
            break

    daily = pd.DataFrame(rows)
    trades_df = pd.DataFrame(trades)
    stats = {
        "liquidation_count": int(n_liq),
        "fee_paid": float(fee_paid),
        "funding_paid": float(funding_paid),
        "avg_turnover": float(turnover_sum / max(len(daily), 1)),
        "final_equity": float(daily["equity"].iloc[-1]) if len(daily) else 0.0,
    }
    return daily, trades_df, stats
