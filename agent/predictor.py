"""个股量化预测（stock_predict CLI 的轻量封装）。

借鉴 stock_gui.py 的 get_prediction：
- 调 CLI 的 analyze(quick=True, defer_signals=True) 拿 T+1/T+5/T+10 分位预测
- 压成紧凑 dict，供盘前摘要、决策上下文和 LLM 工具共用
- 结果按 TTL 落盘缓存（默认 30 分钟），失败静默降级
- CLI 的 analyze 会切换全局指标上下文，用进程内锁串行化

注意：quick=True 只算 L1 自身形态样本，速度快；完整 L2 样本池留给
桌面/GUI。这里的预测用于“参考”，不替代候选评分与风控。
"""
import json
import os
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE_DIR, "sim", "state", "predict_cache.json")
STATE_PATH = os.path.join(BASE_DIR, "sim", "state", "stock_account.json")
DEFAULT_TTL = int(os.getenv("AIQUANT_PREDICT_TTL", "1800"))
DEFAULT_TIMEOUT = int(os.getenv("AIQUANT_PREDICT_TIMEOUT", "45"))

_LOCK = threading.Lock()
_MEM = {}
_ACTIVE = {"thread": None}


def _now():
    return time.time()


def _r(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _pct(value, digits=1):
    try:
        return round(float(value) * 100, digits)
    except (TypeError, ValueError):
        return None


def normalize_code(code):
    """000725 / sz000725 / 000725.SZ -> CLI 可识别的完整代码。"""
    raw = str(code or "").strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("代码为空")
    if raw.startswith(("sh", "sz", "bj", "hk", "us", "fund", "crypto")):
        return raw
    if "." in raw:
        num, suffix = raw.split(".", 1)
        if suffix in ("sh", "sz", "bj"):
            return suffix + num
    try:
        from data.stock import cli_bridge as bridge
        return bridge.load().normalize_code(raw)
    except Exception:
        pass
    if raw.startswith("6"):
        return "sh" + raw
    if raw.startswith(("0", "3")):
        return "sz" + raw
    if raw.startswith(("4", "8")):
        return "bj" + raw
    return raw


def _load_disk():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("entries"), dict):
            return doc
    except (OSError, ValueError):
        pass
    return {"version": 1, "entries": {}}


def _save_disk(doc):
    try:
        entries = doc.get("entries") or {}
        cutoff = _now() - 7 * 86400
        doc["entries"] = {
            k: v for k, v in entries.items()
            if isinstance(v, dict) and float(v.get("ts") or 0) >= cutoff
        }
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_PATH)
    except (OSError, ValueError, TypeError):
        pass


def _cache_get(code, ttl):
    hit = _MEM.get(code)
    if hit and _now() - hit[0] < ttl:
        return hit[1]
    doc = _load_disk()
    ent = (doc.get("entries") or {}).get(code)
    if isinstance(ent, dict) and _now() - float(ent.get("ts") or 0) < ttl:
        summary = ent.get("summary")
        if isinstance(summary, dict):
            _MEM[code] = (_now(), summary)
            return summary
    return None


def _cache_put(code, summary):
    _MEM[code] = (_now(), summary)
    doc = _load_disk()
    doc.setdefault("entries", {})[code] = {
        "ts": _now(), "summary": summary}
    _save_disk(doc)


def _prediction_summary(res):
    """把 CLI analyze 结果压成决策/摘要可用的紧凑结构。"""
    q = res.get("quote") or {}
    tp = res.get("t_pred") or {}
    cl = tp.get("cl") or {}
    t5 = res.get("t5_pred") or {}
    t5cl = t5.get("cl") or {}
    act = res.get("action") or {}
    samples = res.get("samples") or []
    vals = [s.get("n1_cl") for s in samples if s.get("n1_cl") is not None]
    sigs = [{"date": d, "type": t, "reason": x}
            for _, d, t, x in (res.get("signals") or [])[-3:]]
    multi = []
    for d in res.get("multi_pred") or []:
        if d.get("day") in (1, 3, 5, 10):
            mcl = d.get("cl") or {}
            mhi = d.get("hi") or {}
            mlo = d.get("lo") or {}
            multi.append({
                "day": d.get("day"),
                "label": d.get("label") or f"T+{d.get('day')}",
                "cum_pct": _r((d.get("cum_cl") or 0) * 100),
                "price": _r(d.get("price_cl")),
                "price_hi": _r(d.get("price_hi")),
                "price_lo": _r(d.get("price_lo")),
                "cl50": _r(mcl.get(50)),
                "hi75": _r(mhi.get(75)),
                "lo25": _r(mlo.get(25)),
                "up_prob": _pct(d.get("up_prob")),
            })
    multi_by_day = {m.get("day"): m for m in multi}
    t10 = multi_by_day.get(10) or {}
    vals_sorted = sorted(vals)
    ghosts = []
    for g in res.get("ghosts") or []:
        if isinstance(g, dict):
            ghosts.append({
                "date": g.get("date"),
                "open": _r(g.get("open")),
                "close": _r(g.get("close")),
                "high": _r(g.get("high")),
                "low": _r(g.get("low")),
            })
    chips = res.get("chips") or {}
    strat = res.get("strategy") or {}
    conf = tp.get("confidence") or {}
    return {
        "code": res.get("full_code") or "",
        "name": q.get("name") or "",
        "sector": res.get("sector_name") or res.get("industry") or "",
        "price": _r(q.get("price")),
        "score": act.get("score"),
        "verdict": act.get("verdict"),
        "t1_p50": _r(cl.get(50)),
        "t1_p10": _r(cl.get(10)),
        "t1_p90": _r(cl.get(90)),
        "t1_up_prob": _pct(tp.get("up_prob")),
        "t5_p50": _r(t5cl.get(50)),
        "t5_p25": _r(t5cl.get(25)),
        "t5_p75": _r(t5cl.get(75)),
        "t5_up_prob": _pct(t5.get("up_prob")),
        "t10_p50": _r(t10.get("price")),
        "t10_up_prob": t10.get("up_prob"),
        "multi": multi,
        "ghosts": ghosts,
        "anchor": res.get("anchor") or "",
        "next_label": res.get("next_label") or "",
        "today_close": _r(cl.get(50)),
        "today_up_prob": _pct(tp.get("up_prob")),
        "signals": sigs,
        "samples": {
            "n": len(vals),
            "up_rate": (round(len([v for v in vals if v > 0]) / len(vals)
                              * 100, 1) if vals else None),
            "avg_n1_pct": (_pct(sum(vals) / len(vals)) if vals else None),
            "p25_pct": (_pct(vals_sorted[len(vals_sorted) // 4])
                        if len(vals_sorted) >= 4 else None),
            "p75_pct": (_pct(vals_sorted[(len(vals_sorted) * 3) // 4])
                        if len(vals_sorted) >= 4 else None),
        },
        "confidence": {
            "score": _r(conf.get("score"), 2),
            "level": conf.get("level"),
            "samples": conf.get("total_samples"),
        } if conf else None,
        "chips": {
            "avg_cost": _r(chips.get("avg_cost")),
            "profit_pct": _pct(chips.get("profit")),
            "support": _r(chips.get("sup")),
            "resistance": _r(chips.get("res")),
        },
        "vr": _r(res.get("vr_now")),
        "regime": res.get("cur_regime"),
        "anchor": res.get("anchor") or "",
        "strategy": strat.get("label") or res.get("sel_algo") or "",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _analyze_capture(code, timeout):
    """带异常捕获的分析（线程内保存结果/异常）。"""
    from data.stock import cli_bridge as bridge

    cli = bridge.load()
    box = {}

    def work():
        try:
            box["result"] = cli.analyze(
                code, quick=True, defer_signals=True)
        except BaseException as e:
            box["error"] = e

    thread = threading.Thread(target=work, daemon=True)
    with _LOCK:
        _ACTIVE["thread"] = thread
        thread.start()
        thread.join(timeout if timeout and timeout > 0 else None)
    if thread.is_alive():
        raise TimeoutError(f"{code} 预测超时>{timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def predict(code, timeout=DEFAULT_TIMEOUT, force=False, ttl=DEFAULT_TTL):
    """返回单只股票预测摘要；失败抛异常（调用方决定是否降级）。"""
    full = normalize_code(code)
    if not force:
        hit = _cache_get(full, ttl)
        if hit:
            return hit
    res = _analyze_capture(full, timeout)
    if not res:
        raise RuntimeError(f"{full} 无预测结果")
    summary = _prediction_summary(res)
    _cache_put(full, summary)
    return summary


def predict_many(codes, timeout=DEFAULT_TIMEOUT, ttl=DEFAULT_TTL,
                 stop_on_timeout=True):
    """批量预测；单只失败跳过，超时默认停止后续（避免 CLI 状态竞争）。"""
    out = []
    for code in codes:
        try:
            out.append(predict(code, timeout=timeout, ttl=ttl))
        except TimeoutError:
            if stop_on_timeout:
                raise
        except Exception:
            continue
    return out


def predict_map(codes, **kw):
    return {p["code"]: p for p in predict_many(codes, **kw) if p.get("code")}


def _holdings():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        return list((state.get("lots") or {}).keys())
    except (OSError, ValueError):
        return []


def _stock_cfg():
    try:
        import yaml
        with open(os.path.join(BASE_DIR, "config", "stock.yaml"),
                  encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("stock") or {}
    except Exception:
        return {}


def watchlist(max_stocks=10):
    """当前持仓优先，其次候选；返回 [(code, source, name, sector)]。"""
    from data.stock import cli_bridge as bridge

    cfg = _stock_cfg()
    u = cfg.get("universe") or {}
    try:
        cands = bridge.candidates(
            top_n=10, max_scan=u.get("max_scan", 120),
            min_bars=u.get("min_bars", 400),
            min_price=u.get("min_price", 2.0),
            recent_days=u.get("recent_days", 15),
            risk_mode=cfg.get("risk_mode", "稳健"),
            include_etf=u.get("include_etf", False),
            etf_min_price=u.get("etf_min_price", 0.5),
            etf_max_scan=u.get("etf_max_scan", 30),
        )
    except Exception:
        cands = []
    info = {}
    for c in cands:
        info[str(c.get("code") or "").lower()] = (
            c.get("name") or "", c.get("industry") or "")
    out, seen = [], set()
    for code in _holdings():
        key = str(code or "").lower()
        if key and key not in seen:
            seen.add(key)
            name, sector = info.get(key, ("", ""))
            out.append((key, "holding", name, sector))
    stocks = [c for c in cands if c.get("kind") != "etf"]
    etfs = [c for c in cands if c.get("kind") == "etf"]
    for c in stocks + etfs:
        key = str(c.get("code") or "").lower()
        if key and key not in seen:
            seen.add(key)
            out.append((key, "candidate", c.get("name") or "",
                        c.get("industry") or ""))
    return out[:max(0, int(max_stocks))]


def format_prediction(p):
    """一行紧凑文本，供 prompt/工具返回。"""
    if not p:
        return ""
    bits = [
        f"{p.get('code', '')} {p.get('name', '')}"
        f"（{p.get('sector') or '-'}）",
        f"评分{p.get('score')}" if p.get("score") is not None else "",
        f"T+1中位{p.get('t1_p50')}" if p.get("t1_p50") is not None else "",
        (f"P10~P90 {p.get('t1_p10')}~{p.get('t1_p90')}"
         if p.get("t1_p10") is not None else ""),
        (f"上行概率{p.get('t1_up_prob')}%"
         if p.get("t1_up_prob") is not None else ""),
        f"T+5中位{p.get('t5_p50')}" if p.get("t5_p50") is not None else "",
        (f"T+5上行{p.get('t5_up_prob')}%"
         if p.get("t5_up_prob") is not None else ""),
        (f"T+10中位{p.get('t10_p50')} T+10上行{p.get('t10_up_prob')}%"
         if p.get("t10_p50") is not None else ""),
        p.get("verdict") or "",
    ]
    return " ".join(x for x in bits if x)
