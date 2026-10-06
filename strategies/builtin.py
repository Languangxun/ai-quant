"""strategies.builtin: 4 内置策略 (桌面算法同步版).

信号口径统一: T 日收盘生成 Signal(code, BUY/SELL, score, reason, signal_date),
T+1 由 backtest/stock_engine 或 trading/stock_executor 按 A股规则成交
(100股整手/T+1/涨跌停不成交/费用), 与现有 stock_engine._process_signals 一致.

来源标注 (均为作者本人桌面项目, MIT 或自研; 第三方 MIT 见 THIRD_PARTY_NOTICES):
- chaodi_trend <- chaodi_strategies/et_engine/strategies.py trend_breakout +
  boll_breakout (MA60上方+60日新高+量比>=2 / 突破布林上轨+量比>=1.5).
- morphology_tier <- stock_predict/stock_gui.py _composite_signals 三档引擎
  (稳健/均衡/激进, 12类消融信号).
- lgbm_prob <- sm701/lgbm_train_backtest.py (44维 LightGBM P(上涨)).
- mix_ensemble <- 以上三者按权重融合 (morph 0.5/chaodi 0.3/ml 0.2, ml缺失时重归一).
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
