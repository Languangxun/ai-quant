"""sim.multi_run: 基金/港/美/加密/期货 sm701 模拟盘 (无LLM, Pi友好).

每个交易日(加密每日)跑一次:
  bars(<=今日, 尾200) -> sm701_universal TopK -> 按 target 金额调仓
  (T+0 当日可卖, fund T+1 确认), 状态落 sim/state/multi-{market}.json.

用法:
  python -m sim.multi_run --market hk --top 5 --limit 200
  python -m sim.multi_run --market all          # fund+hk+us+crypto+fut
  python -m sim.multi_run --market fund --dry-run
"""
import argparse
import json
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(BASE_DIR, "sim", "state")


def _markets_cfg():
    try:
        import yaml
        with open(os.path.join(BASE_DIR, "config", "markets.yaml"),
                  encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("markets") or {}
    except Exception:
        return {}


def _universe(market, limit, force=False):
    if market == "fund":
        from data.fund import bars as fb
        return fb.universe()
    from data import unified as u
    from data.universe_filter import liquid_top
    all_u = u.universe(market)
    # 非交易日闸门：加密 7x24 直跑，其余市场周末/节假日不产生交易/状态写入
    if market != "crypto" and not force:
        try:
            from trading.calendar import TradingCalendar as _TC
            import datetime as _dt
            if not _TC().is_trading_day(_dt.date.today()):
                print(f"[{market}] 非交易日，跳过（休市无交易）")
                return {"market": market, "skipped": "non-trading-day"}
        except Exception:
            pass
    # 美股 P1: 优先用桌面推送的信号清单 (标普500), 其次 qfq 全量
    if market == "us":
        codes = _us_p1_signal_codes()
        if codes:
            return [(c, c.split(":", 1)[-1]) for c in codes]
        if u.has_us_p1():
            return u.us_p1_universe()
    if market == "crypto":
        return liquid_top(market, all_u,
                          lambda c, tail=150: u.get_bars(c, tail=tail),
                          limit=0)
    return liquid_top(market, all_u,
                      lambda c, tail=150: u.get_bars(c, tail=tail),
                      limit=limit or 200)


def _bars(market, mcode, tail=200):
    if market == "fund":
        from data.fund import bars as fb
        return fb.get_bars(mcode, tail=tail)
    from data import unified as u
    if market == "us" and u.has_us_p1():
        # P1 需 250 日长周期 warmup, 实盘统一取 340 根
        return u.get_us_p1_bars(mcode, tail=max(tail or 0, 340))
    return u.get_bars(mcode, tail=tail)


def _snap_tail(market):
    """P1 美股需 340 根; 其余保持 200 (与原回测/实盘同口径)."""
    if market == "us":
        try:
            from data import unified as u
            if u.has_us_p1():
                return 340
        except Exception:
            pass
    return 200


def _load_state(market, capital, fees):
    from trading.multi_account import MultiAccount
    p = os.path.join(STATE_DIR, f"multi-{market}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return MultiAccount.from_state(json.load(f)), p
        except Exception:
            pass
    return MultiAccount(capital, commission_rate=fees.get("commission_rate", 0),
                        min_commission=fees.get("min_commission", 0),
                        stamp_tax_rate=fees.get("stamp_tax_rate", 0),
                        transfer_fee_rate=fees.get("transfer_fee_rate", 0)), p




def _sm702_crypto_signals(topk=3, threshold=0.55, date_limit=None):
    """sm702 加密模型信号源（walk-forward stop_overlay 输出）。

    读 logs/sm702_crypto_signal.csv（由桌面机每日推送），
    取 prob>=threshold 的 TopK，转成 core Signal 对象。
    CSV 缺失 / 日期过旧 返回 []（由调用方回退 sm701_universal）。"""
    import csv as _csv
    import re as _re
    from datetime import datetime as _dt, timedelta as _td
    from core.types import Signal
    path = "/home/lan/ai-quant/logs/sm702_crypto_signal.csv"
    if not os.path.exists(path):
        return []
    newest = None
    try:
        with open(path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                d = row.get("date") or ""
                if d and (not newest or d > newest):
                    newest = d
    except Exception:
        return []
    if not newest:
        return []
    try:
        gdate = _dt.strptime(newest, "%Y-%m-%d").date()
    except ValueError:
        return []
    # 信号允许 3 天内（模型跑批日数据滞后容忍）
    if _dt.now().date() - gdate > _td(days=3):
        print(f"[crypto] sm702 信号过旧({newest})，跳过", flush=True)
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                p = float(row.get("prob") or 0)
                d = row.get("date") or ""
                if d != newest or p < threshold:
                    continue
                raw = (row.get("code") or "").upper()
                m = _re.match(r"^(.+?)(USDT|BUSD|BTC|ETH)$", raw)
                sym = (m.group(1) + "/" + m.group(2)) if m else raw
                out.append(Signal(
                    code="crypto:" + sym, action="BUY",
                    score=float(p),
                    reason=f"sm702 crypto P={p:.4f}(stop_overlay)",
                    signal_date=d or newest))
    except Exception as e:
        print(f"[crypto] sm702 信号读取失败 {type(e).__name__}: {e}", flush=True)
        return []
    out.sort(key=lambda s: -(s.score or 0))
    return out[:max(1, int(topk))]


def _us_p1_signal_path():
    return os.path.join(BASE_DIR, "logs", "us_p1_signal.csv")


def _us_p1_signal_codes():
    """信号 CSV 里的 us:CODE 列表 (供 _universe 覆盖, 缺文件返回 [])."""
    p = _us_p1_signal_path()
    if not os.path.exists(p):
        return []
    out = []
    try:
        import csv as _csv
        with open(p, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                c = (row.get("code") or "").strip()
                if c.startswith("us:"):
                    out.append(c)
    except Exception:
        return []
    return out


def _us_p1_signal_date():
    p = _us_p1_signal_path()
    if not os.path.exists(p):
        return ""
    newest = ""
    try:
        import csv as _csv
        with open(p, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                d = row.get("date") or ""
                if d > newest:
                    newest = d
    except Exception:
        return ""
    return newest


def _us_p1_signals(topk=5, threshold=0.0, date_limit=None):
    """美股 P1 信号源 (桌面 gen_us_p1_artifacts.py 每日推送).

    读 logs/us_p1_signal.csv; 取最新日 prob TopK.
    缺失/过旧返回 [] (调用方回退 sm701_universal)."""
    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td
    from core.types import Signal
    p = _us_p1_signal_path()
    if not os.path.exists(p):
        return []
    newest = None
    try:
        with open(p, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                d = row.get("date") or ""
                if d and (not newest or d > newest):
                    newest = d
    except Exception:
        return []
    if not newest:
        return []
    try:
        gdate = _dt.strptime(newest, "%Y-%m-%d").date()
    except ValueError:
        return []
    if _dt.now().date() - gdate > _td(days=5):
        print(f"[us] us_p1 信号过旧({newest})，回退 sm701", flush=True)
        return []
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if (row.get("date") or "") != newest:
                    continue
                prob = float(row.get("prob") or 0)
                if prob < threshold:
                    continue
                code = (row.get("code") or "").strip()
                if not code.startswith("us:"):
                    code = "us:" + code
                out.append((prob, Signal(
                    code=code, action="BUY", score=round(prob * 100, 1),
                    reason=f"us_p1 P={prob:.4f}", signal_date=newest)))
    except Exception as e:
        print(f"[us] us_p1 信号读取失败 {type(e).__name__}: {e}", flush=True)
        return []
    out.sort(key=lambda t: -t[0])
    return [s for _, s in out[:max(1, int(topk))]]


def run_one(market, top=5, limit=200, dry_run=False, threshold=None,
            force=False):
    from strategies import builtin as _b  # noqa: 注册策略
    from strategies import registry
    from trading.instruments import MarketModel
    mcfg = _markets_cfg().get(market) or {}
    if not mcfg.get("enabled", True):
        print(f"[{market}] disabled, 跳过")
        return {"market": market, "skipped": True}
    fees = mcfg.get("fees") or {}
    capital = float(mcfg.get("capital", 20000))
    max_pos = int(top or mcfg.get("max_positions", 5))
    max_pct = float(mcfg.get("max_position_pct", 20))
    thr = float(threshold if threshold is not None
                else (mcfg.get("sm701") or {}).get(
                    "threshold", 0.52 if market == "fund" else 0.55))
    min_bars = int((mcfg.get("sm701") or {}).get("min_bars", 65))
    # limit 未给时读 yaml max_scan (fut 0=全扫), 兜底 200
    if not limit:
        limit = int(mcfg.get("max_scan") or 0) or 200
    uni = _universe(market, limit, force=force)
    if isinstance(uni, dict):
        return uni
    print(f"[{market}] 候选 {len(uni)} 只 thr={thr} top={max_pos}")
    bars_map, names = {}, {}
    for mcode, name in uni:
        try:
            bs = _bars(market, mcode)
        except Exception:
            continue
        if len(bs) >= min_bars:
            bars_map[mcode] = bs
            names[mcode] = name
    if not bars_map:
        print(f"[{market}] 无有效K线")
        return {"market": market, "empty": True}
    strat = registry.get("sm701_universal")
    today = max(b[-1].date for b in bars_map.values())
    snap = {c: bs[-_snap_tail(market):] for c, bs in bars_map.items()}
    sigs = []
    if market == "us":
        try:
            sd = _us_p1_signal_date()
            p1 = _us_p1_signals(topk=max_pos, threshold=float(thr or 0.0),
                                date_limit=today)
            # 仅当桌面信号日期与本地行情同日才用 CSV (桌面全历史特征);
            # 否则用本地 qfq + 2026 折模型实时重算, 避免用旧信号配新价.
            if p1 and sd == today:
                print(f"[us] us_p1 信号 {len(p1)} 只覆盖（桌面推送 {sd}）",
                      flush=True)
                sigs = p1
            elif p1:
                print(f"[us] 桌面信号 {sd} != 行情 {today}, 本地模型重算",
                      flush=True)
        except Exception as _e:
            print(f"[us] us_p1 信号失败 {_e}", flush=True)
    if not sigs:
        sigs = strat.generate(snap, {"date": today, "threshold": thr,
                                     "topk": max_pos})
    if market == "crypto":
        try:
            override = _sm702_crypto_signals(topk=max_pos, threshold=thr,
                                             date_limit=today)
            if override:
                print(f"[crypto] sm702 信号 {len(override)} 只覆盖退出灌排", flush=True)
                sigs = override
        except Exception as _e:
            print(f"[crypto] sm702 信号失败 {_e}", flush=True)
    want = [s for s in sigs[:max_pos]]
    _fmt = (lambda v: f"{v:.3f}") if market == "crypto" else (
        (lambda v: f"{v:.2f}") if market == "us" else (lambda v: f"{v:.0f}"))
    print(f"[{market}] {today} 信号 {len(sigs)} Top{max_pos}: " +
          ", ".join(f"{s.code} {_fmt(s.score)}" for s in want))
    acc, path = _load_state(market, capital, fees)
    model = MarketModel()
    px = {c: bs[-1].close for c, bs in bars_map.items() if bs[-1].close}
    total = acc.total_asset(px)
    per = min(total * max_pct / 100.0, total / max_pos) if max_pos else 0
    min_order = float(mcfg.get("min_order_amount", 10))
    min_hold = int(mcfg.get("min_hold", 5 if market != "fund" else 1))
    wantset = {s.code for s in want}
    done = []

    def _held_days(code):
        try:
            ds = [l.buy_date[:10] for l in acc.lots.get(code, [])]
            if not ds:
                return 999
            d0 = min(ds)
            from datetime import date as _d
            return (_d.fromisoformat(today[:10]) - _d.fromisoformat(d0)).days
        except Exception:
            return 999

    # 卖出不在 TopK 的 (持有满 min_hold 天才卖, 防日切磨损)
    for c in list(acc.position_codes()):
        if c not in wantset and _held_days(c) >= min_hold:
            p = px.get(c)
            if not p or dry_run:
                continue
            try:
                t = acc.sell(c, p, acc.available_shares(c, today), today,
                             reason="跌出TopK",
                             stamp_tax_rate=(None if model.classify(
                                 c).stamp_tax else 0.0),
                             transfer_fee_rate=(None if model.classify(
                                 c).transfer_fee else 0.0))
                done.append(t.to_dict())
            except Exception as e:
                print(f"  卖 {c} 失败: {e}")
    # 买入补足
    for s in want:
        c = s.code
        if c in acc.lots or c not in px:
            continue
        if len(acc.position_codes()) >= max_pos or dry_run:
            continue
        p = px[c]
        if per < min_order or not p:
            continue
        inst = model.classify(c, names.get(c, ""))
        if inst.kind == "crypto":
            sh = round(per / p, 6)
        elif inst.kind == "fund":
            sh = round(per / p, 4)
        else:
            sh = int(per / p)
        if sh <= 0:
            continue
        sellable = today if inst.t0 else today  # fund T+1 确认, 简化同日记, 次日可卖
        # fund 次日可卖: sellable 设 today 仍会被当日卖出逻辑误卖?
        # MultiAccount 按日期比较, 同日<=today 即解锁; 为保 T+1, fund 延一天.
        if not inst.t0:
            sellable = today + "+1"  # 字符串比较大于 today, 次日解锁
        try:
            t = acc.buy(c, p, sh, today, sellable,
                        reason=s.reason, confidence=s.score / 100.0,
                        transfer_fee_rate=(None if inst.transfer_fee else 0.0))
            done.append(t.to_dict())
        except Exception as e:
            print(f"  买 {c} 失败: {e}")
    snap_out = acc.snapshot(px)
    print(f"[{market}] 总资产 {snap_out['total_asset']} 现金 {snap_out['cash']} "
          f"成交 {len(done)} 笔")
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = acc.to_state()
        # 状态页 market_account 优先读 total_asset; to_state 本身不含,
        # 补上当日估值, 否则持仓时网站会把现金误当总资产.
        state["total_asset"] = snap_out["total_asset"]
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
        daily = os.path.join(BASE_DIR, "memory", "daily",
                             f"multi-{market}-{today}.json")
        os.makedirs(os.path.dirname(daily), exist_ok=True)
        with open(daily, "w", encoding="utf-8") as f:
            json.dump({"date": today, "market": market,
                       "signals": [{"code": s.code, "score": s.score,
                                    "reason": s.reason} for s in want],
                       "trades": done, "account": snap_out},
                      f, ensure_ascii=False, indent=2)
    return {"market": market, "date": today, "signals": len(sigs),
            "trades": done, "account": snap_out}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="all",
                    help="fund/hk/us/crypto/fut/all")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0,
                    help="缺省读 markets.yaml max_scan (兜底200)")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="非交易日也强制跑 (手动补跑/测试用)")
    a = ap.parse_args(argv)
    mkts = (["fund", "hk", "us", "crypto", "fut"]
            if a.market == "all" else [a.market])
    out = []
    for m in mkts:
        out.append(run_one(m, top=a.top or None, limit=a.limit,
                           dry_run=a.dry_run, threshold=a.threshold,
                           force=a.force))
    return out


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2)[:2000])
