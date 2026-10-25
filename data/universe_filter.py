"""data.universe_filter: 跨市场成分质量过滤 (防复权跳空/新股/垃圾数据).

背景: hk 2026-07~10 回测出现单日 +576x (hk:49988) 等脏数据,
等权基准被带到 +1140%, TopK 追涨后 -98%. A股库经 CLI 清洗较干净,
但 global_daily (港美加密) 为原始快照, 需在选股层加三道闸:
  1. 最小价格 (hk>=1, us>=2, crypto/fund/fut 不限)
  2. 近 N 日最大单日 |涨跌| <= max_ret (hk/us 0.30, crypto 0.50, fund 不限)
  3. 最小 K 线数 (默认 120, 新股/次新直接剔除; crypto/fund 保持 65)
  4. 港股剔除 4xxxx 代码/无名称标的 (窝轮/衍生品/退市残留)

用法:
  from data.universe_filter import liquid_top, is_clean
"""
MIN_BARS = {"hk": 120, "us": 120, "crypto": 65, "fund": 65, "cn": 120,
            "fut": 120}
MIN_PRICE = {"hk": 1.0, "us": 2.0, "crypto": 0.0, "fund": 0.0, "cn": 2.0,
             "fut": 0.0}
# fut 放宽: 主力连续换月跳空可 >30%, 不按脏数据剔除
MAX_RET = {"hk": 0.30, "us": 0.30, "crypto": 0.50, "fund": 99.0, "cn": 0.25,
           "fut": 0.99}


def max_abs_ret(bars, lookback=120):
    mx = 0.0
    seq = bars[-lookback:] if len(bars) > lookback else bars
    for i in range(1, len(seq)):
        p0, p1 = seq[i - 1].close, seq[i].close
        if p0 and p0 > 0 and p1:
            r = abs(p1 / p0 - 1)
            if r > mx:
                mx = r
    return mx


def is_clean(market, bars, lookback=120):
    if not bars or len(bars) < MIN_BARS.get(market, 120):
        return False
    if (bars[-1].close or 0) < MIN_PRICE.get(market, 0):
        return False
    if max_abs_ret(bars, lookback) > MAX_RET.get(market, 0.3):
        return False
    return True


def liquid_top(market, candidates, get_bars, limit=0, lookback=120):
    """candidates=[(mcode,name)] -> 按近期成交额取前limit (已清洗).

    get_bars(mcode, tail)->[Bar]. crypto/fut/fund 不按成交额排序
    (fut 全 82 品种主力连续全要; crypto/fund 数量少).
    """
    if market in ("crypto", "fund", "fut"):
        out = []
        for mcode, name in candidates:
            try:
                b = get_bars(mcode, tail=lookback + 30)
            except Exception:
                continue
            if is_clean(market, b, lookback):
                out.append((mcode, name))
        return out[:limit] if limit else out
    scored = []
    for mcode, name in candidates:
        if market == "hk":
            raw = str(mcode).split(":", 1)[-1]
            if not name or raw.startswith("4"):
                continue
        try:
            b = get_bars(mcode, tail=lookback + 30)
        except Exception:
            continue
        if not is_clean(market, b, lookback):
            continue
        seg = b[-20:]
        dv = sum((x.close or 0) * (x.vol or 0) for x in seg) / max(1, len(seg))
        if dv <= 0:
            continue
        scored.append((dv, mcode, name))
    scored.sort(reverse=True)
    out = [(m, n) for _, m, n in scored]
    return out[:limit] if limit else out
