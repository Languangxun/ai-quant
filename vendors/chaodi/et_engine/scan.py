# -*- coding: utf-8 -*-
"""扫描引擎：本地全市场跑 18 个策略。"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from . import data, indicators, signals, strategies, universe

_MIN_BARS = 250


def market_date(min_bars=_MIN_BARS):
    """库内「最新交易日」：取个股最后日期的众数（避免少数已更新代码抬高）。"""
    row = data._cx().execute(
        "SELECT date, COUNT(*) c FROM (SELECT code, MAX(date) date "
        "FROM daily_bars GROUP BY code HAVING COUNT(*)>=?) "
        "GROUP BY date ORDER BY c DESC LIMIT 1", (min_bars,)).fetchone()
    return row[0] if row else None


def _stale(last_date, market, days=3):
    from datetime import date, timedelta
    try:
        y, m, d = (int(x) for x in last_date.split("-"))
        y2, m2, d2 = (int(x) for x in market.split("-"))
        return date(y, m, d) < date(y2, m2, d2) - timedelta(days=days)
    except Exception:
        return False


def _prepare(local, meta, limit=500, as_of=None, bars_provider=None,
             lite=True, mkt_date=None):
    bars = (bars_provider or data.load_bars)(local, limit=limit)
    if not bars or len(bars["dates"]) < _MIN_BARS:
        return None
    if as_of is None and mkt_date and _stale(bars["dates"][-1], mkt_date):
        return None
    name = (meta.get(local) or {}).get("name", "")
    ind = indicators.compute(bars, lite=lite)
    signals.compute(bars, ind, symbol=bars["symbol"], name=name)
    # 换手率：优先用刷新时反推的总股本，退回 成交额/总市值
    info = meta.get(local) or {}
    shares = info.get("shares") or 0.0
    if shares > 0:
        with np.errstate(invalid="ignore", divide="ignore"):
            ind["turnover_rate"] = ind["volume"] * 100.0 / shares * 100.0
    else:
        cap = info.get("mktcap") or 0.0
        if cap > 0:
            ind["turnover_rate"] = ind["amount"] / cap * 100.0
    dates = bars["dates"]
    if as_of:
        if dates[-1] < as_of:
            return None
        idx = len(dates) - 1
        while idx > 0 and dates[idx] > as_of:
            idx -= 1
        if dates[idx] != as_of:
            return None
    else:
        idx = len(dates) - 1
    return {"local": local, "symbol": bars["symbol"], "name": name,
            "date": dates[idx], "idx": idx, "ind": ind,
            "listing": dates[0], "bars": len(dates)}


def _candidate(rec, meta, spec, params):
    ind, i = rec["ind"], rec["idx"]
    basic = spec["basic_filter"] or strategies.DEFAULT_BASIC
    ok, _reason = universe.apply_basic(meta, rec["local"], ind, i, basic,
                                       rec["listing"], scan_date=rec["date"])
    if not ok:
        return None
    try:
        mask = spec["filter"](ind, params)
    except Exception:
        return None
    mask = np.asarray(mask)
    if mask.ndim == 0:
        mask = np.full(len(ind["close"]), bool(mask))
    if not bool(mask[i]):
        return None
    fields = {"symbol": rec["symbol"], "local": rec["local"],
              "name": rec["name"], "date": rec["date"],
              "board": data.board_of(rec["local"]),
              "industry": (meta.get(rec["local"]) or {}).get("industry", ""),
              "mktcap": (meta.get(rec["local"]) or {}).get("mktcap") or 0.0}
    for k in ("close", "open", "prev_close", "change_pct", "amount",
              "turnover_rate", "vol_ratio_5d", "amplitude",
              "momentum_3d", "momentum_5d", "momentum_10d", "momentum_20d",
              "momentum_30d", "momentum_60d", "rsi_14", "ma5", "ma10",
              "ma20", "ma60", "annual_vol_20d", "atr_14",
              "consecutive_limit_ups", "consecutive_limit_downs"):
        v = ind[k][i]
        fields[k] = float(v) if np.isfinite(v) else None
    for k in ("signal_limit_up", "signal_limit_down", "signal_macd_golden",
              "signal_macd_dead", "signal_ma_golden_5_20",
              "signal_ma_dead_5_20", "signal_ma20_breakout",
              "signal_ma20_breakdown", "signal_n_day_high",
              "signal_n_day_low", "signal_boll_breakout_upper",
              "signal_boll_breakdown_lower", "signal_volume_surge",
              "signal_broken_limit_up", "signal_limit_down_recovery"):
        fields[k] = bool(ind[k][i])
    alerts = []
    for a in spec.get("alerts") or []:
        f = a.get("field")
        if f in ind:
            v = ind[f][i]
            if not np.isfinite(v):
                continue
            hit = False
            op = a.get("op")
            if op == "<":
                hit = v < a["value"]
            elif op == ">":
                hit = v > a["value"]
            else:
                hit = bool(v)
            if hit:
                alerts.append(a.get("message", f))
    fields["alerts"] = alerts
    return fields


def _score(rows, weights):
    if not rows:
        return rows
    for f, w in weights.items():
        vals = np.array([r.get(f) if r.get(f) is not None else np.nan
                         for r in rows], dtype=np.float64)
        finite = np.isfinite(vals)
        if finite.sum() == 0:
            for r in rows:
                r["score"] = (r.get("score") or 0.0) + 0.0
            continue
        mn, mx = vals[finite].min(), vals[finite].max()
        norm = np.zeros(len(rows))
        if mx > mn:
            norm[finite] = (vals[finite] - mn) / (mx - mn)
        else:
            norm[finite] = 0.5      # 单候选时服务端取 50 分
        for r, nv in zip(rows, norm):
            r["score"] = (r.get("score") or 0.0) + w * nv
    for r in rows:
        r["score"] = round(float(r.get("score") or 0.0) * 100.0, 4)
    return rows


def _auto_processes():
    import os
    n = os.cpu_count() or 1
    return max(1, min(8, n))


def _mp_chunk(args):
    """子进程 worker：跑一批代码，返回 (候选行, 跳过数, 处理数)。"""
    codes, sid, params, as_of, mkt_date, min_bars, limit_bars = args
    data.reopen()
    meta = universe.load_meta()
    spec = strategies.get(sid)
    rows, skipped = [], 0
    for code in codes:
        try:
            rec = _prepare(code, meta, as_of=as_of, limit=limit_bars,
                           lite=True, mkt_date=mkt_date)
        except Exception:
            rec = None
        if rec is None:
            skipped += 1
            continue
        r = _candidate(rec, meta, spec, params)
        if r is None:
            skipped += 1
        else:
            rows.append(r)
    return rows, skipped, len(codes)


def _mp_chunk_many(args):
    codes, specs, as_of, mkt_date, min_bars, limit_bars = args
    data.reopen()
    meta = universe.load_meta()
    out = {sid: [] for sid, _ in specs}
    skipped = 0
    for code in codes:
        try:
            rec = _prepare(code, meta, as_of=as_of, limit=limit_bars,
                           lite=True, mkt_date=mkt_date)
        except Exception:
            rec = None
        if rec is None:
            skipped += 1
            continue
        for sid, params in specs:
            r = _candidate(rec, meta, strategies.get(sid), params)
            if r is not None:
                out[sid].append(r)
    return out, skipped, len(codes)


def _run_mp(fn, chunks, processes, progress, total):
    """spawn 进程池跑分块任务，返回聚合结果。"""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed
    ctx = mp.get_context("spawn")
    n = len(chunks)
    done = 0
    with ProcessPoolExecutor(max_workers=processes, mp_context=ctx) as ex:
        futs = [ex.submit(fn, c) for c in chunks]
        results = []
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if progress:
                progress(f"扫描 {min(total, done * (total // max(1, n)))}/{total}")
    return results


def scan(strategy_id, params=None, as_of=None, codes=None, limit=None,
         min_bars=_MIN_BARS, workers=1, progress=None, meta=None,
         bars_provider=None, lite=True, processes=0):
    """跑单个策略，返回 (rows, stats)。

    processes>1 且未注入自定义 meta/bars_provider 时用多进程（spawn）加速。
    """
    spec = strategies.get(strategy_id)
    p = strategies.defaults(spec)
    if params:
        p.update({k: v for k, v in params.items() if k in p})
    meta_injected = meta is not None
    if meta is None:
        meta = universe.load_meta()
    if codes is None:
        codes = [c for c, _n, _last, _name in
                 data.list_universe(min_bars=min_bars)]
    else:
        codes = [data.to_local(c) for c in codes]
    mkt_date = as_of or market_date(min_bars)
    rows, skipped, scanned = [], 0, 0

    use_mp = (processes and processes > 1 and not meta_injected
              and bars_provider is None and len(codes) >= 200)
    def _single():
        nonlocal rows, skipped, scanned
        def work(code):
            try:
                rec = _prepare(code, meta, as_of=as_of,
                               bars_provider=bars_provider, lite=lite,
                               mkt_date=mkt_date)
            except Exception:
                return None
            if rec is None:
                return None
            return _candidate(rec, meta, spec, p)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, c): c for c in codes}
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r is None:
                    skipped += 1
                else:
                    rows.append(r)
                if progress and done % 200 == 0:
                    progress(f"扫描 {done}/{len(codes)}，命中 {len(rows)}")
        scanned = done

    if use_mp:
        try:
            procs = min(int(processes), _auto_processes())
            per = max(60, (len(codes) + procs * 4 - 1) // (procs * 4))
            chunks = [codes[i:i + per] for i in range(0, len(codes), per)]
            args = [(c, strategy_id, p, as_of, mkt_date, min_bars, 500)
                    for c in chunks]
            results = _run_mp(_mp_chunk, args, procs, progress, len(codes))
            for r, sk, cnt in results:
                rows.extend(r)
                skipped += sk
                scanned += cnt
        except Exception as e:
            if progress:
                progress(f"多进程不可用({e})，回退单进程")
            rows, skipped, scanned = [], 0, 0
            _single()
    else:
        _single()
    _score(rows, spec["scoring"])
    rows.sort(key=lambda r: (r.get("score") or 0.0), reverse=True)
    total_passed = len(rows)
    lim = limit or spec.get("limit") or 100
    rows = rows[:lim]
    stats = {"strategy": strategy_id, "scanned": scanned,
             "skipped": skipped, "passed": total_passed,
             "returned": len(rows)}
    return rows, stats


def scan_many(strategy_ids, params_by_id=None, as_of=None, codes=None,
              limit=None, min_bars=_MIN_BARS, progress=None, meta=None,
              bars_provider=None, lite=True, processes=0):
    """一次遍历跑多个策略（行情只加载/计算一次），返回 {sid: (rows, stats)}。"""
    specs = [strategies.get(s) for s in strategy_ids]
    meta_injected = meta is not None
    if meta is None:
        meta = universe.load_meta()
    if codes is None:
        codes = [c for c, _n, _last, _name in
                 data.list_universe(min_bars=min_bars)]
    else:
        codes = [data.to_local(c) for c in codes]
    cands = {s["id"]: [] for s in specs}
    mkt_date = as_of or market_date(min_bars)
    skipped = 0
    done = 0

    spec_params = []
    for spec in specs:
        p = strategies.defaults(spec)
        if params_by_id and params_by_id.get(spec["id"]):
            p.update({k: v for k, v in params_by_id[spec["id"]].items()
                      if k in p})
        spec_params.append((spec["id"], p))

    use_mp = (processes and processes > 1 and not meta_injected
              and bars_provider is None and len(codes) >= 200)
    def _single():
        nonlocal skipped, done
        for code in codes:
            done += 1
            try:
                rec = _prepare(code, meta, as_of=as_of,
                               bars_provider=bars_provider, lite=lite,
                               mkt_date=mkt_date)
            except Exception:
                rec = None
            if rec is None:
                skipped += 1
            else:
                for spec, (sid, p) in zip(specs, spec_params):
                    r = _candidate(rec, meta, spec, p)
                    if r is not None:
                        cands[sid].append(r)
            if progress and done % 500 == 0:
                progress(f"扫描 {done}/{len(codes)}")

    if use_mp:
        try:
            procs = min(int(processes), _auto_processes())
            per = max(60, (len(codes) + procs * 4 - 1) // (procs * 4))
            chunks = [codes[i:i + per] for i in range(0, len(codes), per)]
            args = [(c, spec_params, as_of, mkt_date, min_bars, 500)
                    for c in chunks]
            results = _run_mp(_mp_chunk_many, args, procs, progress,
                              len(codes))
            for res, sk, cnt in results:
                for sid, rows in res.items():
                    cands[sid].extend(rows)
                skipped += sk
                done += cnt
        except Exception as e:
            if progress:
                progress(f"多进程不可用({e})，回退单进程")
            cands = {s["id"]: [] for s in specs}
            skipped = done = 0
            _single()
    else:
        _single()
    out = {}
    for spec in specs:
        rows = cands[spec["id"]]
        _score(rows, spec["scoring"])
        rows.sort(key=lambda r: (r.get("score") or 0.0), reverse=True)
        total_passed = len(rows)
        lim = limit or spec.get("limit") or 100
        rows = rows[:lim]
        out[spec["id"]] = (rows, {
            "strategy": spec["id"], "scanned": done, "skipped": skipped,
            "passed": total_passed, "returned": len(rows)})
    return out
