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
"""
import os

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_CANDIDATES = [
    os.path.join(_BASE, "vendors", "sm701", "lgbm_model.txt"),
    os.path.expanduser("~/桌面/sm701/lgbm_output/lgbm_model.txt"),
]
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


def predict_proba(feature_rows):
    """feature_rows: [dict 44维]. 返回 [P(上涨)]. 无模型/无包时返回 []."""
    if not available() or not feature_rows:
        return []
    path = model_path()
    if not path:
        return []
    import lightgbm as lgb
    import numpy as np
    bst = lgb.Booster(model_file=path)
    X = [[float(r.get(k, 0.0)) for k in FEATURES_SM701]
         for r in feature_rows]
    return [float(x) for x in bst.predict(np.array(X, dtype=np.float32))]
