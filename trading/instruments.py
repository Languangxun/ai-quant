"""市场规则层（MarketModel）：按标的类型给出交易规则。

借鉴 akquant 的 MarketModel（可插拔 A股 T+1 / 期货 T+0）思路，把
「这个标的怎么交易」从执行器里抽出来，股票与 ETF 共用一套流程：

- 股票：T+1；涨跌停 10%（创业板 sz30 / 科创板 sh68 / 北交所 20%，
  主板 ST 5%）；卖出印花税 + 过户费。
- ETF/LOF：默认 T+1，跨境/债券/黄金/货币/商品类为 T+0；
  免印花税与过户费；创业板/科创板相关 ETF 涨跌停 20%，其余 10%。

规则可用 config/stock.yaml 的 market.etf 段覆盖：
    market:
      etf:
        t0_keywords: [...]        # 覆盖内置关键词
        t0_codes: [sz159941]      # 显式 T+0 代码
        t0_all: false             # true=所有 ETF 按 T+0
        limit_pct_overrides: {sh510300: 0.10}
"""
from dataclasses import dataclass

STOCK_LIMIT_PCT = 0.10
ETF_LIMIT_PCT = 0.10
WIDE_LIMIT_PCT = 0.20
ST_LIMIT_PCT = 0.05
DEFAULT_LOT_SIZE = 100

# 内置 T+0 ETF 关键词（跨境/债券/黄金/货币/商品）
DEFAULT_ETF_T0_KEYWORDS = (
    "跨境", "恒生", "恒指", "港股", "H股", "中概", "海外", "亚太", "美国",
    "纳指", "纳斯达克", "标普", "道琼斯", "日经", "德国", "法国", "英国",
    "沙特", "东南亚", "QDII", "债", "国债", "政金", "城投", "信用",
    "可转债", "转债", "黄金", "白银", "豆粕", "有色", "能源化工", "原油",
    "商品", "货币", "现金", "短融",
)
# 内置 T+0 ETF 代码前缀：沪 511债券/货币、513跨境、518黄金
DEFAULT_ETF_T0_PREFIXES = ("sh511", "sh513", "sh518")
WIDE_LIMIT_KEYWORDS = ("创业板", "科创")


def is_etf_code(code):
    """ETF/LOF 代码：沪 51/56/58，深 15/16/18 开头。"""
    c = str(code or "").lower()
    pre = c[2:4] if len(c) >= 4 else ""
    return pre in ("51", "56", "58", "15", "16", "18")


@dataclass(frozen=True)
class Instrument:
    """单个标的的交易规则（不可变，可缓存）。"""
    code: str
    name: str = ""
    kind: str = "stock"            # stock / etf
    t0: bool = False               # True=当日可卖（T+0）
    limit_pct: float = STOCK_LIMIT_PCT
    stamp_tax: bool = True         # 卖出是否收印花税
    transfer_fee: bool = True      # 买卖是否收过户费
    lot_size: int = DEFAULT_LOT_SIZE

    @property
    def t_plus_1(self):
        return not self.t0

    def to_dict(self):
        return {
            "code": self.code, "name": self.name, "kind": self.kind,
            "t0": self.t0, "limit_pct": self.limit_pct,
            "stamp_tax": self.stamp_tax, "transfer_fee": self.transfer_fee,
            "lot_size": self.lot_size,
        }


class MarketModel:
    """按配置给出标的规则；classify 结果只依赖 (code, name)。"""

    def __init__(self, cfg=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        etf_cfg = (cfg.get("market") or {}).get("etf") or {}
        self.etf_t0_all = bool(etf_cfg.get("t0_all", False))
        keywords = etf_cfg.get("t0_keywords")
        self.etf_t0_keywords = tuple(
            keywords if keywords is not None else DEFAULT_ETF_T0_KEYWORDS)
        prefixes = etf_cfg.get("t0_prefixes")
        self.etf_t0_prefixes = tuple(
            str(p).lower() for p in
            (prefixes if prefixes is not None else DEFAULT_ETF_T0_PREFIXES))
        self.etf_t0_codes = {
            str(c).lower() for c in (etf_cfg.get("t0_codes") or [])}
        self.limit_overrides = {
            str(k).lower(): float(v)
            for k, v in (etf_cfg.get("limit_pct_overrides") or {}).items()
        }
        self.lot_size = int(cfg.get("lot_size") or DEFAULT_LOT_SIZE)

    # ---------- 规则判定 ----------

    def _etf_t0(self, code, name):
        if self.etf_t0_all or code in self.etf_t0_codes:
            return True
        if code.startswith(self.etf_t0_prefixes):
            return True
        if not name:
            return False
        return any(k in name for k in self.etf_t0_keywords)

    def _limit_pct(self, code, name, kind):
        if code in self.limit_overrides:
            return self.limit_overrides[code]
        if kind == "etf":
            if code.startswith(("sh588", "sh589")):
                return WIDE_LIMIT_PCT
            if any(k in name for k in WIDE_LIMIT_KEYWORDS):
                return WIDE_LIMIT_PCT
            return ETF_LIMIT_PCT
        if code.startswith(("sz30", "sh68", "bj")):
            return WIDE_LIMIT_PCT
        if "ST" in name.upper():
            return ST_LIMIT_PCT
        return STOCK_LIMIT_PCT

    def classify(self, code, name=""):
        code = str(code or "").lower()
        name = str(name or "")
        kind = "etf" if is_etf_code(code) else "stock"
        return Instrument(
            code=code, name=name, kind=kind,
            t0=(kind == "etf" and self._etf_t0(code, name)),
            limit_pct=self._limit_pct(code, name, kind),
            stamp_tax=(kind != "etf"),
            transfer_fee=(kind != "etf"),
            lot_size=self.lot_size,
        )


_DEFAULT_MODEL = MarketModel()


def price_limit_pct(code, name=""):
    """兼容旧接口：涨跌停幅度（0.10 / 0.20 / 0.05）。"""
    return _DEFAULT_MODEL.classify(code, name).limit_pct
