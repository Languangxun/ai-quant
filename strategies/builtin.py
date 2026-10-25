"""strategies.builtin: 5 内置策略 (桌面算法同步版).

信号口径统一: T 日收盘生成 Signal(code, BUY/SELL, score, reason, signal_date),
T+1 由 backtest/stock_engine 或 trading/stock_executor 按 A股规则成交
(100股整手/T+1/涨跌停不成交/费用), 与现有 stock_engine._process_signals 一致.
多市场 (fund/hk/us/crypto) 由 sm701_universal + trading.multi_account 承接,
T+0/无涨跌停/小数份额, 规则见 trading/instruments.MarketModel.

来源标注 (均为作者本人桌面项目, MIT 或自研; 第三方 MIT 见 THIRD_PARTY_NOTICES):
- chaodi_trend <- chaodi_strategies/et_engine/strategies.py trend_breakout +
  boll_breakout (MA60上方+60日新高+量比>=2 / 突破布林上轨+量比>=1.5).
- morphology_tier <- stock_predict/stock_gui.py _composite_signals 三档引擎
  (稳健/均衡/激进, 12类消融信号).
- lgbm_prob <- sm701/lgbm_train_backtest.py (44维 LightGBM P(上涨)).
- mix_ensemble <- 以上三者按权重融合 (morph 0.5/chaodi 0.3/ml 0.2, ml缺失时重归一).
- sm701_universal <- sm701 44维全量特征 (factors/sm701.py) 跨市场复用,
  基金/港/美/加密暂代因子 (无专用因子前统一用此, 阈值按市场可配).
"""
from core.strategy import BaseStrategy, register
from core.types import Signal
from factors import technical as ft


def _rows(bars):
    return [{"date": b.date, "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "vol": b.vol} for b in bars]


@register
class ChaodiTrend(BaseStrategy):
    name = "chaodi_trend"
    description = "通达信趋势突破 (MA60+60日新高+量比, Boll突破 fallback)"

    def generate(self, bars_by_code, ctx=None):
        out = []
        date = (ctx or {}).get("date", "")
        for code, bars in (bars_by_code or {}).items():
            if len(bars) < 65:
                continue
            closes = [b.close for b in bars]
            vols = [b.vol for b in bars]
            ind = ft.compute(closes, vols=vols)
            if not ind:
                continue
            c = closes[-1]
            ma60 = sum(closes[-60:]) / 60
            hi60 = max(closes[-60:-1]) if len(closes) > 60 else max(closes[:-1])
            v5 = sum(vols[-5:]) / 5 if sum(vols[-5:]) else 1.0
            vr = (vols[-1] / v5) if v5 else 1.0
            ma5, ma20 = ind["ma5"][-1], ind["ma20"][-1]
            score, why = 0, ""
            if c > (ma60 or 0) and c > (hi60 or 0) and vr >= 2.0:
                score, why = 80, f"趋势突破 收{ c:.2f}>60日高{hi60:.2f} 量比{vr:.1f}"
            elif (ma5 and ind["boll_up"][-1]
                  and c > ind["boll_up"][-1] and vr >= 1.5):
                score, why = 70, f"布林突破 量比{vr:.1f}"
            elif (ma5 and ma20 and ma5 > ma20 and vr >= 1.2
                  and c > (ma20 or 0)):
                score, why = 55, f"均线多头 MA5>MA20 量比{vr:.1f}"
            if score:
                out.append(Signal(code=code, action="BUY", score=score,
                                  reason=why, signal_date=date or bars[-1].date))
        out.sort(key=lambda s: -s.score)
        return out


@register
class MorphologyTier(BaseStrategy):
    name = "morphology_tier"
    description = "形态相似度三档引擎 (stock_predict CLI 同源)"

    def generate(self, bars_by_code, ctx=None):
        ctx = ctx or {}
        risk_mode = ctx.get("risk_mode", "稳健")
        from factors import morphology as mp
        sigs = mp.bars_signals(bars_by_code, risk_mode)
        out = []
        for code, lst in sigs.items():
            for i, sdate, act, reason in lst[-1:]:
                # 只取 T 日 (ctx date 或 bars 尾日) 的信号, 保证因果
                want = ctx.get("date")
                if want and sdate != want:
                    # bars 尾日即 T 日时放行
                    bars = bars_by_code.get(code) or []
                    if not bars or bars[-1].date != sdate:
                        continue
                if act not in ("BUY", "SELL"):
                    continue
                out.append(Signal(code=code, action=act,
                                  score=float(mp.score_of(reason)),
                                  reason=reason, signal_date=sdate))
        out.sort(key=lambda s: -s.score)
        return out


@register
class LgbmProb(BaseStrategy):
    name = "lgbm_prob"
    description = "sm701 LightGBM 上涨概率 (缺模型时空信号)"

    def generate(self, bars_by_code, ctx=None):
        from factors import ml
        if not ml.available() or not ml.model_path():
            return []
        # 特征组装复用 technical 12 列 + 动量 (44 维中可算部分, 缺失填 0;
        # 完整 44 维需 sm701 全量 pipeline, 这里做轻量近似, 阈值 0.55)
        feats = []
        codes = []
        for code, bars in (bars_by_code or {}).items():
            if len(bars) < 65:
                continue
            closes = [b.close for b in bars]
            ind = ft.compute(closes)
            if not ind or ind["rsi14"][-1] is None:
                continue
            r = {"ret1": closes[-1] / closes[-2] - 1 if closes[-2] else 0,
                 "ret5": closes[-1] / closes[-6] - 1 if len(closes) > 6 else 0,
                 "rsi14": ind["rsi14"][-1] or 50,
                 "macd_hist": ind["macd_hist"][-1] or 0}
            feats.append(r)
            codes.append((code, bars[-1].date))
        probs = ml.predict_proba(feats)
        out = []
        for (code, d), p in zip(codes, probs):
            if p >= 0.55:
                out.append(Signal(code=code, action="BUY",
                                  score=round(p * 100, 1),
                                  reason=f"LGBM P(涨)={p:.3f}",
                                  signal_date=d))
        out.sort(key=lambda s: -s.score)
        return out


@register
class MixEnsemble(BaseStrategy):
    name = "mix_ensemble"
    description = "融合: 形态0.5 + 超短0.3 + LGBM0.2 (缺失重归一)"

    def generate(self, bars_by_code, ctx=None):
        subs = {"morphology_tier": 0.5, "chaodi_trend": 0.3,
                "lgbm_prob": 0.2}
        agg = {}
        for sname, w in subs.items():
            strat = __import__("strategies", fromlist=["registry"]
                               ).registry.get(sname)
            if not strat:
                continue
            try:
                sigs = strat.generate(bars_by_code, ctx)
            except Exception:
                continue
            if not sigs and sname == "lgbm_prob":
                continue
            for s in sigs:
                if s.action != "BUY":
                    continue
                a = agg.setdefault(s.code, {"score": 0.0, "why": [],
                                            "date": s.signal_date})
                a["score"] += s.score * w
                a["why"].append(f"{sname}:{s.score:.0f}")
        # ml 缺失时重归一 (0.5+0.3=0.8 -> /0.8)
        from factors import ml as _ml
        renorm = 1.0 if (_ml.available() and _ml.model_path()) else 0.8
        out = [Signal(code=c, action="BUY",
                      score=round(v["score"] / renorm, 1),
                      reason=" ".join(v["why"]), signal_date=v["date"])
               for c, v in agg.items()]
        out.sort(key=lambda s: -s.score)
        return out


def _market_of(code):
    c = str(code or "").lower()
    if c.startswith("crypto:"):
        return "crypto"
    if c.startswith("us:"):
        return "us"
    if c.startswith("hk:"):
        return "hk"
    if c.startswith("fut:"):
        return "fut"
    if c.startswith("fund:") or (len(c) == 6 and c.isdigit()):
        return "fund"
    return "cn"


@register
class Sm701Universal(BaseStrategy):
    name = "sm701_universal"
    description = ("sm701 跨市场复用: cn 44维单模型, hk/us/fut 自动切 production 分市场模型, fund/crypto 暂代")

    def generate(self, bars_by_code, ctx=None):
        from factors import ml as _ml
        from factors import sm701 as _s
        if not _ml.available() or not _ml.model_path():
            return []
        ctx = ctx or {}
        # 阈值: ctx.threshold 优先, 否则按市场默认 (fund 0.52, 其余 0.55),
        # 也可 ctx.thresholds={market: thr} 批量覆写 (config/markets.yaml 同值).
        thr_map = dict(ctx.get("thresholds") or {})
        try:
            import yaml as _y, os as _o
            mp = _o.path.join(_o.path.dirname(_o.path.dirname(
                _o.path.abspath(__file__))), "config", "markets.yaml")
            if _o.path.exists(mp):
                with open(mp, encoding="utf-8") as f:
                    cfg = (_y.safe_load(f) or {}).get("markets") or {}
                for m, mc in cfg.items():
                    thr_map.setdefault(m, float((mc.get("sm701") or {})
                                                .get("threshold", 0.55)))
        except Exception:
            pass
        default_thr = ctx.get("threshold", None)
        default_thr = float(default_thr) if default_thr is not None else None
        # 市场环境: 同批 codes 的当日 ret 均值/上涨占比 (mkt/breadth),
        # 横截面 rank (r_*) 按当日值分位; 单只/基金无对手时回退 0/0.5.
        closes_by = {}
        for code, bars in (bars_by_code or {}).items():
            if bars and len(bars) >= 2:
                closes_by[code] = [b.close for b in bars]
        last_dates = {}
        for code, bars in (bars_by_code or {}).items():
            if bars:
                last_dates[code] = bars[-1].date
        # 按 date 分组算市场均值 (同 date 才可比, 跨市场日历不同天然分组)
        rets = {}
        for code, bars in (bars_by_code or {}).items():
            if len(bars) < 61:
                continue
            cs = [b.close for b in bars]
            d = bars[-1].date
            g = rets.setdefault(d, {"r1": [], "r5": [], "r20": [],
                                    "ma20r": [], "vr20": [], "vol20": [],
                                    "logvol": [], "rsi": []})
            c0 = cs[-1]
            r1 = c0 / cs[-2] - 1 if cs[-2] else 0.0
            r5 = c0 / cs[-6] - 1 if len(cs) > 6 and cs[-6] else 0.0
            r20 = c0 / cs[-21] - 1 if len(cs) > 21 and cs[-21] else 0.0
            ma20 = sum(cs[-20:]) / 20
            ma20r = c0 / ma20 - 1 if ma20 else 0.0
            g["r1"].append((code, r1))
            g["r5"].append((code, r5))
            g["r20"].append((code, r20))
            g["ma20r"].append((code, ma20r))
        mkt = {}
        for d, g in rets.items():
            r1v = [v for _, v in g["r1"]]
            m1 = sum(r1v) / len(r1v) if r1v else 0.0
            # ret5/20 均值近似 mkt5/20 (T日截面, 与训练 mkt 口径同向)
            m5 = sum(v for _, v in g["r5"]) / len(g["r5"]) if g["r5"] else 0.0
            m20 = sum(v for _, v in g["r20"]) / len(g["r20"]) if g["r20"] else 0.0
            br = sum(1 for v in r1v if v > 0) / len(r1v) if r1v else 0.5

            def _rank(lst, code):
                vs = sorted(v for _, v in lst)
                cur = dict(lst).get(code, 0.0)
                le = sum(1 for v in vs if v <= cur)
                return le / max(1, len(vs))
            for code, _ in g["r1"]:
                mkt[code] = {"mkt1": m1, "mkt5": m5, "mkt20": m20,
                             "breadth5": br,
                             "ranks": {
                                 "ret5": _rank(g["r5"], code),
                                 "ret20": _rank(g["r20"], code),
                                 "ret60": 0.5,
                                 "ma20r": _rank(g["ma20r"], code),
                                 "rsi14": 0.5, "vr20": 0.5,
                                 "vol20": 0.5, "logvol": 0.5}}
        feats, codes = [], []
        for code, bars in (bars_by_code or {}).items():
            if len(bars) < 65:
                continue
            row = _s.bars_to_row(bars, mkt.get(code))
            if row is None:
                continue
            feats.append(row)
            codes.append((code, bars[-1].date, _market_of(code)))
        # 分市场自动识别: 每市场各自选模型 (hk/us/fut production 三周期,
        # cn/fund/crypto 44维单模型), 该市场无模型的只数不出信号 (宁缺勿滥)
        probs = [None] * len(feats)
        by_m = {}
        for i, (_c, _d, m) in enumerate(codes):
            by_m.setdefault(m, []).append(i)
        for m, idxs in by_m.items():
            ps = _ml.predict_proba([feats[i] for i in idxs], market=m)
            for i, p in zip(idxs, ps):
                probs[i] = p
        # 阈值过滤 (默认不过度补足: 域外推时低于阈值硬买曾导致 hk -96%,
        # 宁可空仓. 需 TopK 强制覆盖时传 ctx {"fill_below_thr": True,
        # "topk": K}, 理由标 TopK补足).
        try:
            topk = int(ctx.get("topk", 0) or 0)
        except Exception:
            topk = 0
        fill = bool(ctx.get("fill_below_thr", False))
        out, below = [], []
        for (code, d, m), p in zip(codes, probs):
            if p is None:
                continue  # 该市场无可用模型
            if default_thr is not None:
                thr = default_thr  # ctx 显式阈值全局优先 (测试/回测覆写用)
            else:
                thr = float(thr_map.get(m, 0.52 if m == "fund" else 0.55))
            sig = Signal(code=code, action="BUY",
                         score=round(p * 100, 1),
                         reason=f"sm701[{m}] P={p:.3f}>=thr{thr:.2f}",
                         signal_date=d)
            if p >= thr:
                out.append(sig)
            else:
                below.append((p, Signal(
                    code=code, action="BUY", score=round(p * 100, 1),
                    reason=f"sm701[{m}] P={p:.3f}<thr{thr:.2f}(TopK补足)",
                    signal_date=d)))
        out.sort(key=lambda s: -s.score)
        if fill and topk and len(out) < topk:
            below.sort(key=lambda t: -t[0])
            out += [s for _, s in below[:topk - len(out)]]
            out.sort(key=lambda s: -s.score)
        elif topk:
            out = out[:topk]
        return out
