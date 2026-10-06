"""backtest.engine_v2: 策略可插拔回测 (兼容 stock_engine 口径).

沿用 backtest.stock_engine.StockBacktest 的成交/费用/ATR止损/基准实现
(T+1/整手/佣金万2.5/印花税/过户费/涨跌停不成交), 只是把第一遍「扫描信号」
换成 strategies.registry 任一策略的 generate(), 便于做 A/B:
  morphology_tier (原 CLI 口径, 默认) vs chaodi_trend vs mix_ensemble.

用法:
  from backtest.engine_v2 import run
  run(strategy="mix_ensemble", limit=300, risk_mode="稳健")
"""
import time

from backtest.stock_engine import StockBacktest


class StrategyBacktest(StockBacktest):
    def __init__(self, strategy="morphology_tier", **kw):
        self.strategy_name = strategy
        super().__init__(**kw)

    def scan(self):
        from data import store as st
        from data.stock import cli_bridge as bridge
        from factors import technical as ft
        from factors.morphology import score_of
        codes = bridge.universe(min_bars=self.min_bars, boards=self.boards,
                                max_scan=self.limit or None,
                                include_etf=self.include_etf,
                                etf_min_price=self.etf_min_price,
                                etf_max_scan=self.etf_max_scan)
        total = len(codes)
        self.progress(f"股票池 {total} 只 策略={self.strategy_name}")
        t0 = time.time()
        n_sig = 0
        for k, (code, name, _ind, _m) in enumerate(codes):
            if k % 200 == 0:
                self.progress(f"  扫描 {k}/{total} ({time.time()-t0:.0f}s, "
                              f"信号 {n_sig})")
            try:
                rows = bridge.db_rows(code)
            except Exception:
                continue
            if len(rows) < self.min_bars:
                continue
            for i in range(1, len(rows)):
                p0, p1 = rows[i - 1]["close"], rows[i]["close"]
                if p0 and p1 and p0 > 0:
                    b = self.bench.setdefault(rows[i]["date"], [0.0, 0])
                    b[0] += p1 / p0 - 1.0
                    b[1] += 1
            # --- 按策略 fast-path 生成 (T 日信号 -> T+1 执行, 与原口径一致) ---
            if self.strategy_name == "morphology_tier":
                try:
                    sigs = bridge.signals(rows, self.risk_mode)
                except Exception:
                    continue
                for i, sdate, action, reason in sigs:
                    j = i + 1
                    if j >= len(rows):
                        continue
                    edate = rows[j]["date"]
                    if self.start and edate < self.start:
                        continue
                    if self.end and edate > self.end:
                        continue
                    self.events.setdefault(edate, []).append(
                        (code, action, score_of(reason), reason, name))
                    n_sig += 1
            elif self.strategy_name in ("chaodi_trend", "mix_ensemble"):
                # chaodi 部分: 单遍向量化 (ma60/60日高/量比/布林), 逐 i 查表
                closes = [r["close"] for r in rows]
                vols = [r["vol"] for r in rows]
                try:
                    ind = ft.compute(closes, vols=vols)
                except Exception:
                    ind = {}
                if not ind:
                    continue
                ma5a, upa = ind.get("ma5") or [], ind.get("boll_up") or []
                # morphology 部分仅 mix 需要
                morph = {}
                if self.strategy_name == "mix_ensemble":
                    try:
                        for i, sdate, act, rs in bridge.signals(
                                rows, self.risk_mode):
                            morph[sdate] = (act, score_of(rs), rs)
                    except Exception:
                        morph = {}
                for i in range(60, len(rows) - 1):
                    c = closes[i]
                    if not c:
                        continue
                    ma60 = sum(closes[i - 59:i + 1]) / 60
                    hi60 = max(closes[i - 60:i])
                    v5 = sum(vols[i - 4:i + 1]) / 5 if i >= 4 else vols[i]
                    vr = (vols[i] / v5) if v5 else 1.0
                    cs, why = 0, ""
                    ma5 = ma5a[i] if i < len(ma5a) else None
                    up = upa[i] if i < len(upa) else None
                    if c > ma60 and c > hi60 and vr >= 2.0:
                        cs, why = 80, f"趋势突破 量比{vr:.1f}"
                    elif ma5 and up and c > up and vr >= 1.5:
                        cs, why = 70, f"布林突破 量比{vr:.1f}"
                    else:
                        continue
                    edate = rows[i + 1]["date"]
                    if self.start and edate < self.start:
                        continue
                    if self.end and edate > self.end:
                        continue
                    if self.strategy_name == "chaodi_trend":
                        self.events.setdefault(edate, []).append(
                            (code, "BUY", cs, why, name))
                        n_sig += 1
                    else:
                        m = morph.get(rows[i]["date"])
                        ms = m[1] if m and m[0] == "BUY" else 0
                        blended = round((ms * 0.5 + cs * 0.3) / 0.8, 1)
                        if blended >= 50:
                            self.events.setdefault(edate, []).append(
                                (code, "BUY",
                                 int(blended), f"mix morph{ms:.0f} chaodi{cs}",
                                 name))
                            n_sig += 1
            elif self.strategy_name == "lgbm_prob":
                self.progress("  [warn] lgbm_prob 回测需模型, 本次跳过信号")
            else:
                raise SystemExit(f"未知策略: {self.strategy_name}")
        self.progress(f"扫描完成：{total} 只，信号 {n_sig} 条，"
                      f"耗时 {time.time() - t0:.0f}s")


def run(strategy="mix_ensemble", tag=None, **kw):
    bt = StrategyBacktest(strategy=strategy, **kw)
    bt.run()
    return bt.save(tag or f"v2-{strategy}")
