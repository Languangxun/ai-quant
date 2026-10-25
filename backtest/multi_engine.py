"""backtest.multi_engine: 基金/港/美/加密 sm701 统一回测 (TopK 长持).

口径:
- 信号: strategies.sm701_universal, T日收盘算 P(涨), P>=thr 即候选,
  按 score 取 TopK (K=max_positions), T+1 收盘执行 (与 stock_engine 同 T+1).
- 账户: trading.multi_account.MultiAccount (小数份额), 费用按
  config/markets.yaml 各市场 fees, 无涨跌停校验 (limit=1.0 跳过).
- 退出: 跌出 TopK 或持有满 20 个交易日即 T+1 卖出; 无 ATR 跟踪
  (fund NAV 平滑 / crypto 7x24 另议, v1 先统一 TopK 轮动).
- 基准: 所选市场等权日均收益.

用法:
  from backtest.multi_engine import run
  run("hk", limit=100, topk=5)
  run("fund")  # 10只全量
  python -m backtest.multi_engine --market crypto --topk 3
"""
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, "backtest", "results")


def _markets_cfg():
    try:
        import yaml
        p = os.path.join(BASE_DIR, "config", "markets.yaml")
        with open(p, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("markets") or {}
    except Exception:
        return {}


def _universe(market, limit=0):
    if market == "fund":
        from data.fund import bars as fb
        u = fb.universe()
        return u[:limit] if limit else u
    from data import unified as u
    from data.universe_filter import liquid_top
    all_u = u.universe(market)
    if not all_u:
        return []
    if market == "crypto":
        return liquid_top(market, all_u, lambda c, tail=150: u.get_bars(c, tail=tail),
                          limit=limit)
    return liquid_top(market, all_u, lambda c, tail=150: u.get_bars(c, tail=tail),
                      limit=limit or 200)


def _bars(market, mcode, tail=None):
    if market == "fund":
        from data.fund import bars as fb
        return fb.get_bars(mcode, tail=tail)
    from data import unified as u
    return u.get_bars(mcode, tail=tail)


def _trade_dates(market, start="", end=""):
    if market == "fund":
        from data.fund import bars as fb
        ds = set()
        for mcode, _ in fb.universe():
            for b in fb.get_bars(mcode):
                ds.add(b.date)
        out = sorted(ds)
    else:
        from data import unified as u
        out = u.trade_dates(market)
    if start:
        out = [d for d in out if d >= start]
    if end:
        out = [d for d in out if d <= end]
    return out


def run(market="hk", limit=None, topk=None, threshold=None, start="", end="",
        capital=None, max_hold=None, min_hold=None, progress=print):
    from strategies import builtin as _b  # noqa: 注册表
    from strategies import registry
    from trading.instruments import MarketModel
    from trading.multi_account import MultiAccount

    t0 = time.time()
    mcfg = _markets_cfg().get(market) or {}
    fees = mcfg.get("fees") or {}
    capital = float(capital or mcfg.get("capital", 20000))
    max_pos = int(topk or mcfg.get("max_positions", 5))
    max_pct = float(mcfg.get("max_position_pct", 20))
    if max_hold is None:
        max_hold = int(mcfg.get("max_hold", 20))
    if min_hold is None:
        min_hold = int(mcfg.get("min_hold", 5 if market != "fund" else 1))
    thr = float(threshold if threshold is not None
                else (mcfg.get("sm701") or {}).get(
                    "threshold", 0.52 if market == "fund" else 0.55))
    # limit 未给时读 yaml max_scan (fut 0=全扫), 兜底 200; CLI 默认 None 不再吃掉配置
    if not limit:
        limit = int(mcfg.get("max_scan") or 0) or 200
    uni = _universe(market, limit if market != "fund" else 0)
    if not uni:
        raise SystemExit(f"[{market}] 无成分 (缺 global_daily.db / NAV 拉取失败?)")
    progress(f"[{market}] 成分 {len(uni)} 只 thr={thr} topk={max_pos}")

    # 全量 bars 常驻内存 (hk200x~1500根 / us200 / crypto20 / fund10, Pi 可承受)
    # 质量门槛与选股一致 (hk/us 120根; 脏数据已在 _universe 剔除, 此处双保险)
    from data.universe_filter import MIN_BARS as _MINB
    need = int(_MINB.get(market, 65))
    bars_map, name_map = {}, {}
    for mcode, name in uni:
        try:
            bs = _bars(market, mcode)
        except Exception:
            continue
        if len(bs) >= need:
            bars_map[mcode] = bs
            name_map[mcode] = name
    if not bars_map:
        raise SystemExit(f"[{market}] 有效K线不足{need}根")
    dates = _trade_dates(market, start, end)
    # 信号需65根预热: 起点截到最早可算日
    min_d = min(b.date for bs in bars_map.values() for b in bs[64:65])
    dates = [d for d in dates if d >= min_d]
    if start:
        dates = [d for d in dates if d >= start]
    progress(f"[{market}] 回测 {dates[0]}~{dates[-1]} ({len(dates)}日)")

    acc = MultiAccount(capital, commission_rate=fees.get("commission_rate", 0.0),
                       min_commission=fees.get("min_commission", 0.0),
                       stamp_tax_rate=fees.get("stamp_tax_rate", 0.0),
                       transfer_fee_rate=fees.get("transfer_fee_rate", 0.0))
    model = MarketModel()
    strat = registry.get("sm701_universal")
    equity, trades, bench_eq = [], [], []
    held_days = {}
    prev_rank = {}
    bv = 1.0
    idx = {c: {b.date: k for k, b in enumerate(bs)}
           for c, bs in bars_map.items()}

    for di, day in enumerate(dates):
        if di % 100 == 0:
            progress(f"  {di}/{len(dates)} {day} 持仓{len(acc.position_codes())}")
        # T日截面 bars (<=day, 尾200根加速特征)
        snap = {}
        for c, bs in bars_map.items():
            k = idx[c].get(day)
            if k is None or k < 64:
                continue
            snap[c] = bs[max(0, k - 199):k + 1]
        sigs = []
        if snap:
            try:
                sigs = strat.generate(snap, {"date": day,
                                             "threshold": thr,
                                             "topk": max_pos})
            except Exception:
                sigs = []
        rank = {s.code: s.score for s in sigs}
        # 当日收盘价
        px = {c: bars_map[c][idx[c][day]].close
              for c in snap if idx[c].get(day) is not None}
        # 基准等权 (中位数 + 剔除 |ret|>30% 脏数据, 防 hk 49988 +576x 带偏)
        day_rets = []
        for c, bs in bars_map.items():
            k = idx[c].get(day)
            if k and k >= 1:
                p0, p1 = bs[k - 1].close, bs[k].close
                if p0 and p0 > 0 and p1:
                    r = p1 / p0 - 1
                    if abs(r) <= 0.30:
                        day_rets.append(r)
        if day_rets:
            import statistics as _st
            bv *= 1 + _st.median(day_rets)
        bench_eq.append((day, round(bv, 6)))
        if di == 0:
            equity.append((day, round(capital, 2)))
            continue
        # 执行: 按昨日信号调仓 (T+1). 首日空仓.
        # 卖出: 跌出 TopK 或超 max_hold, 且持有满 min_hold 天 (降频, 防日切磨损)
        want = sorted(prev_rank, key=lambda c: -prev_rank[c])[:max_pos]
        wantset = set(w for w in want if w in px)
        for c in list(acc.position_codes()):
            hd = held_days.get(c, 0) + 1
            held_days[c] = hd
            if ((c not in wantset or hd > max_hold)
                    and hd >= min_hold):
                p = px.get(c)
                if not p:
                    continue
                inst = model.classify(c, name_map.get(c, ""))
                try:
                    t = acc.sell(c, p, acc.available_shares(c, day), day,
                                 reason="跌出TopK/超期",
                                 stamp_tax_rate=(None if inst.stamp_tax else 0.0),
                                 transfer_fee_rate=(None if inst.transfer_fee else 0.0))
                    trades.append(t.to_dict())
                    held_days.pop(c, None)
                except Exception:
                    pass
        # 买入: 等权补足
        total = acc.total_asset(px) if px else capital
        per = min(total * max_pct / 100.0, total / max_pos) if max_pos else 0
        for c in want:
            if c in acc.lots or c not in px:
                continue
            if len(acc.position_codes()) >= max_pos:
                break
            p = px[c]
            if not p or per < float((mcfg.get("min_order_amount", 10))):
                continue
            inst = model.classify(c, name_map.get(c, ""))
            if inst.kind == "crypto":
                sh = round(per / p, 6)
            elif inst.kind == "fund":
                sh = round(per / p, 4)
            elif inst.kind in ("hk", "us"):
                sh = int(per / p)
            else:
                sh = int(per / p)
            if sh <= 0:
                continue
            sellable = day  # T+0 当日可卖; fund T+1 用下一交易日
            if not inst.t0:
                nxt = dates[di + 1] if di + 1 < len(dates) else day
                sellable = nxt if nxt != day else day
            try:
                t = acc.buy(c, p, sh, day, sellable, reason=f"sm701 TopK",
                            transfer_fee_rate=(None if inst.transfer_fee else 0.0))
                trades.append(t.to_dict())
                held_days[c] = 0
            except Exception:
                pass
        prev_rank = rank
        equity.append((day, round(acc.total_asset(px) if px else total, 2)))
    # 指标
    eq = [v for _, v in equity]
    ret = eq[-1] / eq[0] - 1 if eq and eq[0] else 0.0
    n = len(eq)
    ann = (1 + ret) ** (252 / n) - 1 if n > 10 else 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1) if peak else 0.0
    out = {"market": market, "thr": thr, "topk": max_pos, "codes": len(bars_map),
           "start": dates[0] if dates else "", "end": dates[-1] if dates else "",
           "days": n, "ret": round(ret, 4), "ann": round(ann, 4),
           "mdd": round(mdd, 4), "trades": len(trades),
           "final": eq[-1] if eq else capital,
           "bench_ret": round(bench_eq[-1][1] / bench_eq[0][1] - 1, 4)
           if len(bench_eq) > 1 and bench_eq[0][1] else 0.0,
           "min_hold": min_hold, "max_hold": max_hold,
           "elapsed": round(time.time() - t0, 1)}
    tag = f"multi-{market}-top{max_pos}"
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, tag + ".json"), "w",
              encoding="utf-8") as f:
        json.dump({**out, "equity": equity[-252 * 3:],
                   "bench": bench_eq[-252 * 3:]}, f, ensure_ascii=False)
    progress(f"[{market}] {out} 落盘 {tag}.json")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="hk")
    ap.add_argument("--limit", type=int, default=None,
                    help="缺省读 markets.yaml max_scan (兜底200)")
    ap.add_argument("--topk", type=int, default=None,
                    help="缺省读 markets.yaml max_positions")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--min-hold", type=int, default=None,
                    help="缺省读 markets.yaml min_hold")
    a = ap.parse_args()
    print(run(a.market, a.limit, a.topk, a.threshold, a.start, a.end,
              min_hold=a.min_hold))
