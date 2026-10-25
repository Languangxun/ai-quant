"""factors.ml: ML 上涨概率适配器 (sm701 + market-sniper, 懒加载).

来源:
- ~/桌面/sm701/lgbm_train_backtest.py: LightGBM 二分类 P(次日上涨),
  44 维特征 (价量技术 32 + 市场环境 breadth/mkt + 相对强度 r_*),
  训练 2018-2023 / 验证 2024+ 样本外, T日收盘信号 T+1 开盘买 T+1 收盘卖.
  另有 features_extra.py (资金流/两融/龙虎榜/业绩事件, 均错后1日防未来函数),
  train_tcn_pattern.py (TCN 时序), lgbm_tcn_ensemble.py (双模型融合).
- ~/桌面/market-sniper/market_sniper/{features,lgbm_model}.py: 28 维特征
  (动量/均线偏离/MACD/RSI/KDJ/BOLL/ATR/量比/振幅 + peer_rs 行业相对强弱),
  按行业分组训练, 验证集分位数自适应阈值 (入场高分位/出场中位).

Pi 上默认不装 lightgbm (Zero2W 编译重); 本模块 import 失败时 available()=False,
上层策略自动降级为技术/形态分, 不阻塞运行. 需要 ML 时:
  pip install lightgbm  # Pi 上约 10-20 分钟
  并把桌面模型 vendors/sm701/*.txt 拷到 Pi 同路径 (见 vendors/README).

分市场自动识别 (2026-10): hk/us/fut 优先用 sm701 production 三周期
分市场模型 (prod_{hk|us|fut}_h{1,5,10}_tech.txt, 40 维 tech 特征,
三周期等权平均, 与桌面 stock_predict v7 实时复算同口径); 缺文件或
lightgbm 不可用时回退 44 维单模型 (fund/crypto/cn 恒走单模型,
保证 A股历史口径可比). 模型目录候选: vendors/sm701/production,
~/桌面/sm701/production.
"""
import json
import os

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_CANDIDATES = [
    os.path.join(_BASE, "vendors", "sm701", "lgbm_model.txt"),
    os.path.expanduser("~/桌面/sm701/lgbm_output/lgbm_model.txt"),
]
PROD_CANDIDATES = [
    os.path.join(_BASE, "vendors", "sm701", "production"),
    os.path.expanduser("~/桌面/sm701/production"),
]
# 市场 -> production 模型前缀 (cn 保持 44 维单模型口径, 不切 production)
PROD_MPRE = {"hk": "hk_", "us": "us_", "fut": "fut_"}
PROD_HORIZONS = ("1", "5", "10")
FEATURES_SM701 = [
    "ret1", "ret5", "ret10", "ret20", "ret60",
    "ma5r", "ma10r", "ma20r", "ma60r", "ma5_20", "ma20_60",
    "vol20", "vol60", "volr", "rsi6", "rsi14",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "bollpos", "hilo20", "atr_n",
    "vr5", "vr20", "logvol", "amp", "gap", "cpos", "updays20",
    "mkt1", "mkt5", "mkt20", "breadth5",
    "r_ret5", "r_ret20", "r_ret60", "r_ma20r", "r_rsi14",
    "r_vr20", "r_vol20", "r_logvol",
]
_lgb = None


def available():
    global _lgb
    if _lgb is not None:
        return True
    try:
        import lightgbm  # noqa: F401
        _lgb = True
        return True
    except Exception:
        return False


def model_path():
    for p in MODEL_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


_prod_cache = {}


def production_dir(market):
    """该市场 production 三周期模型齐备的目录; 无则空串."""
    mpre = PROD_MPRE.get(market)
    if not mpre:
        return ""
    for d in PROD_CANDIDATES:
        if all(os.path.exists(os.path.join(
                d, f"prod_{mpre}h{h}_tech.txt")) for h in PROD_HORIZONS):
            return d
    return ""


def _prod_set(market):
    """(目录, 市场) 懒加载 production 模型集; 不可用缓存 None."""
    d = production_dir(market)
    if not d:
        return None
    key = (d, market)
    if key in _prod_cache:
        return _prod_cache[key]
    out = None
    try:
        with open(os.path.join(d, "features_tech.json"),
                  encoding="utf-8") as f:
            feats = (json.load(f) or {}).get("features") or []
        if feats and available():
            import lightgbm as lgb
            mpre = PROD_MPRE[market]
            bs = {}
            for h in PROD_HORIZONS:
                b = lgb.Booster(model_file=os.path.join(
                    d, f"prod_{mpre}h{h}_tech.txt"))
                names = list(b.feature_name() or [])
                if names and len(names) == len(feats):
                    feats = names  # 以模型列序为准
                bs[h] = b
            if len(bs) == len(PROD_HORIZONS):
                out = {"feats": feats, "boosters": bs}
    except Exception:
        out = None
    _prod_cache[key] = out
    return out


def predict_proba(feature_rows, market="cn"):
    """feature_rows: [dict 特征]. 返回 [P(上涨)].

    hk/us/fut: production 三周期等权平均 (40 维 tech); 缺模型或该市场
    未配置时回退 44 维单模型. 无任何模型/无包时返回 []."""
    if not available() or not feature_rows:
        return []
    import numpy as np
    prod = _prod_set(market)
    if prod:
        feats = prod["feats"]
        X = np.array([[float(r.get(k, 0.0)) for k in feats]
                      for r in feature_rows], dtype=np.float32)
        ps = [prod["boosters"][h].predict(X) for h in PROD_HORIZONS]
        return [float(x) for x in sum(ps) / len(ps)]
    path = model_path()
    if not path:
        return []
    import lightgbm as lgb
    X = [[float(r.get(k, 0.0)) for k in FEATURES_SM701]
         for r in feature_rows]
    return [float(x) for x in
            lgb.Booster(model_file=path).predict(np.array(X,
                                                         dtype=np.float32))]


def full_row_from_bars(bars, market_ctx=None):
    """[Bar] -> 44维完整特征 (sm701.py, 替代旧 4 维近似).

    fund NAV (vol=0) / 港美加密均可直接输入; 不足 65 根返回 None.
    """
    try:
        from factors import sm701 as _s
        return _s.bars_to_row(bars, market_ctx)
    except Exception:
        return None


def batch_rows(bars_by_code, market_ctx_by_code=None):
    """{code: [Bar]} -> ({code: 44维}, [缺数据的code])."""
    out, missing = {}, []
    for code, bars in (bars_by_code or {}).items():
        ctx = (market_ctx_by_code or {}).get(code)
        row = full_row_from_bars(bars, ctx)
        if row is None:
            missing.append(code)
        else:
            out[code] = row
    return out, missing
