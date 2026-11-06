"""决策期工具集：借鉴 stock_gui.py 的 AI tool calling。

模型在决策时可按需取数，而不是把所有数据一次性塞进 prompt：
- get_market        大盘指数/均线/强弱（agent.market_view）
- get_prediction    个股 T+1/T+5/T+10 预测（agent.predictor -> CLI analyze）
- get_kline         近期日K（CLI 缓存，实际价口径）
- get_quote         实时/最近报价
- get_orderbook     A股五档挂单 + 集合竞价/交易阶段（CLI 7.0.3）
- get_external_env  特朗普/关税/地缘/美联储事件（去重排序后）
- get_etf           ETF 成分股权重/近期表现（借鉴 GUI）
- search_news       个股/主题新闻（DeepSeek Search）

所有工具返回字符串（JSON 或紧凑文本），失败返回 {"error": ...}，
绝不抛异常打断 LLM 决策。工具结果是外部数据，提示词要求忽略其中指令。
"""
import json
import time

from agent import event_watch, market_view, predictor
from models.web_search import SEARCH_NOTE

_EXT_TTL = 900


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def _r(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _fmt_results(results, limit=5):
    lines = []
    for i, r in enumerate((results or [])[:limit], 1):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        summary = str(r.get("summary") or r.get("snippet") or "").strip()
        meta = "，".join(x for x in (
            str(r.get("date") or "")[:10],
            str(r.get("site") or r.get("site_name") or "").strip()) if x)
        line = f"{i}. {title or summary[:50]}"
        if meta:
            line += f"（{meta}）"
        if summary and summary != title:
            line += f"：{summary[:360]}"
        lines.append(line)
    return "\n".join(lines)


class ToolBox:
    def __init__(self, context=None, search_client=None,
                 predict_timeout=45, max_calls=8):
        self.context = context if isinstance(context, dict) else {}
        self.search_client = search_client
        self.predict_timeout = predict_timeout
        self.max_calls = max(1, int(max_calls or 8))
        self.calls = 0
        self.trace = []
        self._ext_cache = {}

    # ---------- 工具定义 ----------
    def specs(self):
        return [
            {"type": "function", "function": {
                "name": "get_market",
                "description": "获取A股大盘简报：上证/深证/创业板/科创50的"
                               "均线位置、近20/60日涨跌与强弱。判断大盘环境、"
                               "是否适合开仓时调用。",
                "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {
                "name": "get_prediction",
                "description": "获取个股形态预测：T+1中位/P10~P90、上行概率、"
                               "T+5、T+10、相似样本统计与近期信号。准备买入"
                               "或需要复核持仓时调用；盘前 predictions 只是"
                               "早盘快照，盘中应以本工具为准。可一次传多只"
                               "（codes，最多4只）横向比较。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string",
                             "description": "股票代码，如 sz002050 / 002050"},
                    "codes": {"type": "array", "items": {"type": "string"},
                              "description": "批量代码，最多4只；与 code 二选一"}}}}},
            {"type": "function", "function": {
                "name": "get_kline",
                "description": "获取个股/ETF近期日K（实际价口径），用于判断"
                               "趋势、支撑压力、量价。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "代码"},
                    "days": {"type": "integer",
                             "description": "最近多少根，5-60，默认20",
                             "default": 20}}}}},
            {"type": "function", "function": {
                "name": "get_quote",
                "description": "获取实时/最近报价（腾讯快照，失败回退缓存收盘）。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "代码"}}}}},
            {"type": "function", "function": {
                "name": "get_orderbook",
                "description": "获取A股五档挂单与集合竞价状态（腾讯优先/新浪"
                               "兜底）：买卖五档价量、委比、涨跌停价、当前阶段"
                               "（集合竞价/竞价撮合/连续竞价/午间休市/收盘竞价）。"
                               "准备下单、判断涨停能否买入或跌停能否卖出时调用。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "A股代码"},
                    "levels": {"type": "integer",
                               "description": "返回档数 1-5，默认5",
                               "default": 5}}}}},
            {"type": "function", "function": {
                "name": "get_external_env",
                "description": "查询外部宏观/地缘环境（特朗普、关税、美联储、"
                               "美国选举、黄金原油、美股港股等），按新鲜度和"
                               "来源质量去重排序，返回近N天事件摘要与信号。"
                               "出现重大事件或需要判断隔夜外盘时调用。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string",
                              "description": "可选聚焦词，如“特朗普 关税”"},
                    "days": {"type": "integer",
                             "description": "近N天，1-30，默认3",
                             "default": 3},
                    "count": {"type": "integer",
                              "description": "返回条数，3-12，默认6",
                              "default": 6}}}}},
            {"type": "function", "function": {
                "name": "get_etf",
                "description": "获取ETF/LOF前十大成分股与权重、当日/5日/20日"
                               "表现。分析ETF、判断行业ETF强弱来源时调用。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string",
                             "description": "ETF代码，如 sh510300"},
                    "limit": {"type": "integer",
                              "description": "成分股数量，5-30，默认10",
                              "default": 10}}}}},
            {"type": "function", "function": {
                "name": "search_news",
                "description": "检索个股/主题近期新闻公告；可传代码或关键词。",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "代码，可选"},
                    "query": {"type": "string",
                              "description": "检索词，可选"}}}}},
        ]

    # ---------- 入口 ----------
    def call(self, name, args=None):
        args = args if isinstance(args, dict) else {}
        if self.calls >= self.max_calls:
            return _json({"error": f"工具调用已达上限 {self.max_calls}"})
        self.calls += 1
        t0 = time.time()
        try:
            fn = getattr(self, f"_tool_{name}", None)
            if fn is None:
                out = _json({"error": f"未知工具 {name}"})
            else:
                out = fn(args)
                if not isinstance(out, str):
                    out = _json(out)
        except Exception as e:
            out = _json({"error": f"{type(e).__name__}: {e}"[:300]})
        self.trace.append({
            "name": name,
            "args": args,
            "elapsed": round(time.time() - t0, 2),
            "error": out.startswith('{"error"'),
        })
        return out[:10000]

    # ---------- 具体工具 ----------
    def _tool_get_market(self, args):
        text = ""
        try:
            text = market_view.brief()
        except Exception:
            text = ""
        pre = self.context.get("premarket") or {}
        if not text:
            text = str(pre.get("market_brief") or "")
        if not text:
            return _json({"error": "暂无市场简报"})
        return _json({
            "market_brief": text[:2500],
            "trend": market_view.trend() if text else "unknown",
            "note": SEARCH_NOTE,
        })

    def _tool_get_prediction(self, args):
        raw = args.get("codes")
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
        codes = [str(x).strip() for x in (raw or []) if str(x).strip()]
        code = str(args.get("code") or "").strip()
        if code and code not in codes:
            codes.insert(0, code)
        codes = list(dict.fromkeys(codes))[:4]

        if not codes:
            pre = self.context.get("premarket") or {}
            preds = pre.get("predictions") or []
            if preds:
                return _json({"predictions": preds[:8],
                              "source": "premarket"})
            codes = [str(c.get("code") or "").strip() for c in
                     (self.context.get("candidates") or [])[:2]]
            codes = [c for c in codes if c][:1]
            if not codes:
                return _json({"error": "缺少 code"})

        if len(codes) == 1:
            p = predictor.predict(codes[0], timeout=self.predict_timeout)
            return _json({"prediction": p})

        preds, errors = [], []
        for c in codes:
            try:
                preds.append(predictor.predict(
                    c, timeout=self.predict_timeout))
            except Exception as e:
                errors.append(f"{c}: {type(e).__name__}: {e}"[:120])
        out = {"predictions": preds}
        if errors:
            out["errors"] = errors
        return _json(out)

    def _tool_get_kline(self, args):
        from data.stock import cli_bridge as bridge

        code = predictor.normalize_code(str(args.get("code") or ""))
        try:
            days = max(5, min(60, int(args.get("days") or 20)))
        except (TypeError, ValueError):
            days = 20
        rows = bridge.db_rows(code, tail=days + 25)
        # 缓存落后于全市场最新交易日时增量刷新（单只硬超时，失败保留缓存）
        try:
            latest = bridge.latest_date()
            if rows and latest and rows[-1].get("date", "") < latest:
                fresh = market_view._with_timeout(
                    lambda: bridge.series(code, min_bars=100), 12)
                if fresh:
                    rows = fresh[-max(days + 25, 60):]
        except Exception:
            pass
        if not rows:
            return _json({"error": f"无日K数据 {code}"})
        prices = []
        for r in rows:
            try:
                prices.append(round(
                    bridge.display_price(code, r["close"]), 3))
            except Exception:
                prices.append(r.get("close"))
        out = []
        for i in range(max(0, len(rows) - days), len(rows)):
            chg = None
            if i > 0 and prices[i - 1]:
                chg = round((prices[i] / prices[i - 1] - 1) * 100, 2)
            out.append({
                "date": rows[i].get("date"),
                "close": prices[i],
                "chg_pct": chg,
                "vol": rows[i].get("vol"),
            })
        ma5 = sum(prices[-5:]) / min(5, len(prices))
        ma20 = sum(prices[-20:]) / min(20, len(prices))
        return _json({
            "code": code,
            "rows": out,
            "ma5": round(ma5, 3),
            "ma20": round(ma20, 3),
        })

    def _tool_get_quote(self, args):
        from data.stock import cli_bridge as bridge

        code = predictor.normalize_code(str(args.get("code") or ""))
        q = {}
        try:
            q = bridge.quote(code) or {}
        except Exception:
            q = {}
        if not q.get("price"):
            rows = bridge.db_rows(code, tail=1)
            if rows:
                q = {"name": "", "price": bridge.display_price(
                    code, rows[-1]["close"]),
                    "prev_close": bridge.display_price(
                        code, rows[-1]["close"]),
                    "stale": True}
        if not q.get("price"):
            return _json({"error": f"无报价 {code}"})
        chg = None
        if q.get("prev_close"):
            chg = round((q["price"] / q["prev_close"] - 1) * 100, 2)
        return _json({
            "code": code, "name": q.get("name") or "",
            "price": q.get("price"), "prev_close": q.get("prev_close"),
            "chg_pct": chg, "stale": bool(q.get("stale")),
        })

    def _tool_get_orderbook(self, args):
        """五档挂单 + 集合竞价状态（借鉴 GUI 盘口面板 / CLI 7.0.3）。"""
        from data.stock import cli_bridge as bridge

        code = predictor.normalize_code(str(args.get("code") or ""))
        try:
            levels = max(1, min(5, int(args.get("levels") or 5)))
        except (TypeError, ValueError):
            levels = 5
        try:
            d = bridge.order_book(code)
        except Exception as e:
            return _json({"error": f"盘口获取失败 {code}: "
                                   f"{type(e).__name__}: {e}"[:200]})
        bids = [[_r(p), _r(v, 0)] for p, v in (d.get("bids") or [])[:levels]]
        asks = [[_r(p), _r(v, 0)] for p, v in (d.get("asks") or [])[:levels]]
        bid1 = bids[0][0] if bids else None
        ask1 = asks[0][0] if asks else None
        spread = (round(ask1 - bid1, 3)
                  if bid1 and ask1 and ask1 >= bid1 else None)
        return _json({
            "code": code,
            "name": d.get("name") or "",
            "phase": d.get("phase") or "",
            "auction": bool(d.get("auction")),
            "time": d.get("time") or "",
            "price": _r(d.get("price")),
            "prev_close": _r(d.get("prev_close")),
            "open": _r(d.get("open")),
            "high": _r(d.get("high")),
            "low": _r(d.get("low")),
            "limit_up": _r(d.get("limit_up")),
            "limit_down": _r(d.get("limit_down")),
            "bids": bids,
            "asks": asks,
            "bid_vol": _r(d.get("bid_vol"), 0),
            "ask_vol": _r(d.get("ask_vol"), 0),
            "wei_bi_pct": _r(d.get("wei_bi")),
            "bid1": bid1,
            "ask1": ask1,
            "spread": spread,
            "stale": False,
        })

    def _tool_get_etf(self, args):
        """ETF 前十大成分股（借鉴 GUI 的 get_etf）。"""
        from data.stock import cli_bridge as bridge

        code = str(args.get("code") or "").strip()
        if not code:
            return _json({"error": "缺少 ETF 代码"})
        try:
            full = predictor.normalize_code(code)
        except Exception as e:
            return _json({"error": f"代码格式不对: {e}"[:120]})
        try:
            if not bridge.is_etf(full):
                return _json({"error": f"{full} 不是 ETF/LOF"})
            limit = max(5, min(30, int(args.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10
        try:
            cli = bridge.load()
            meta, rows = cli.etf_constituent_perf(full, limit)
        except Exception as e:
            return _json({"error": f"成分股获取失败 {full}: "
                                   f"{type(e).__name__}: {e}"[:200]})
        out = []
        for r in (rows or [])[:limit]:
            out.append({
                "code": r.get("code"),
                "name": r.get("name"),
                "weight_pct": _r(r.get("weight")),
                "price": _r(r.get("price")),
                "chg_pct": _r(r.get("chg")),
                "ret5_pct": _r(r.get("ret5")),
                "ret20_pct": _r(r.get("ret20")),
                "volume_ratio": _r(r.get("vr")),
            })
        if not out:
            return _json({"error": f"{full} 暂无成分股数据（可能未回填）"})
        return _json({
            "code": full,
            "report": (meta or {}).get("period") if isinstance(meta, dict) else meta,
            "rows": out,
        })

    def _search(self, query, count=5):
        if not self.search_client:
            return ""
        try:
            results = self.search_client.search(query)
        except Exception as e:
            return f"检索失败: {e}"
        return _fmt_results(results, limit=count)

    def _tool_get_external_env(self, args):
        try:
            days = max(1, min(30, int(args.get("days") or 3)))
        except (TypeError, ValueError):
            days = 3
        try:
            count = max(3, min(12, int(args.get("count") or 6)))
        except (TypeError, ValueError):
            count = 6
        query = str(args.get("query") or "").strip()[:100]
        key = (query, days, count)
        now = time.time()
        hit = self._ext_cache.get(key)
        if hit and now - hit[0] < _EXT_TTL:
            return hit[1]
        if not self.search_client:
            out = _json({"days": days, "results": "联网检索未启用",
                         "note": SEARCH_NOTE})
            self._ext_cache[key] = (now, out)
            return out
        try:
            doc = event_watch.collect(
                self.search_client,
                date_str=time.strftime("%Y-%m-%d"),
                days=days,
                topics=("trump", "macro"),
                max_queries=2 if not query else 1,
                count=max(count, 8),
                limit=count,
                focus=query,
            )
        except Exception as e:
            out = _json({"days": days, "error": f"{type(e).__name__}: {e}"[:200],
                         "note": SEARCH_NOTE})
            self._ext_cache[key] = (now, out)
            return out
        events = []
        for it in (doc.get("items") or [])[:count]:
            events.append({
                "title": str(it.get("title") or "")[:120],
                "summary": str(it.get("summary") or "")[:240],
                "date": it.get("date"),
                "site": it.get("site"),
                "matched": (it.get("matched") or [])[:5],
            })
        out = _json({
            "days": days,
            "focus": query,
            "salience": doc.get("salience"),
            "signals": doc.get("signals"),
            "events": events,
            "results": doc.get("brief") or "未检索到结果",
            "note": SEARCH_NOTE,
        })
        self._ext_cache[key] = (now, out)
        return out

    def _tool_search_news(self, args):
        code = str(args.get("code") or "").strip()
        query = str(args.get("query") or "").strip()
        if not query and code:
            name = ""
            try:
                name = (self._tool_get_quote({"code": code}) or "")
                name = json.loads(name).get("name") or ""
            except Exception:
                name = ""
            query = f"{name} {code} 最新消息 公告 业绩"
        if not query:
            return _json({"error": "缺少 query 或 code"})
        text = self._search(query[:120] + " lang:zh", count=5)
        if not text:
            return _json({"error": "联网检索未启用或未检索到结果"})
        return _json({"query": query[:120], "results": text,
                      "note": SEARCH_NOTE})
