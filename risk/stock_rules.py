"""股票/ETF 预交易风控（独立拦截层）。

借鉴 akquant 的 RiskManager（订单在引擎层被拦截）：executor 只负责把
订单转成成交，所有「该不该成交、成交多少」的判断集中在这里，便于单测
与状态页展示。

校验项：
- 持仓数上限 / 单票仓位上限（占总资产 %）
- 买入金额下限 = min(min_order_amount, 总资产 × min_order_pct%)，小资金自适应
- 100 股整手；目标金额不足一手但单票额度够时按一手兜底
- SELL 仅可卖已解锁份额（T+1 / T+0 由 MarketModel 决定）
- 涨停附近不买、跌停附近不卖
"""
from dataclasses import dataclass

from trading.instruments import MarketModel

STATUS_VALIDATED = "VALIDATED"
STATUS_REJECTED = "REJECTED"
STATUS_NO_CHANGE = "NO_CHANGE"


@dataclass
class RiskDecision:
    """一次预交易校验结果。allowed=False 时 status 为 REJECTED/NO_CHANGE。"""
    allowed: bool
    status: str
    reason: str = ""
    shares: int = 0
    amount: float = 0.0
    instrument: object = None

    def __bool__(self):
        return self.allowed


class StockRiskManager:
    def __init__(self, account, market=None, max_positions=5,
                 max_position_pct=20.0, min_order_amount=5000.0,
                 min_order_pct=5.0, check_limit=True):
        self.account = account
        self.market = market or MarketModel()
        self.max_positions = int(max_positions)
        self.max_position_pct = float(max_position_pct)
        self.min_order_amount = float(min_order_amount)
        self.min_order_pct = float(min_order_pct)
        self.check_limit = check_limit

    # ---------- 通用 ----------

    def min_amount_for(self, total_asset):
        """当前总资产下的单笔最小买入金额（固定上限与比例下限取小）。"""
        return min(self.min_order_amount,
                   float(total_asset) * self.min_order_pct / 100.0)

    def _limit_blocked(self, inst, side, price, prev_close):
        """涨跌停附近禁止成交（涨停买不到 / 跌停卖不出）。"""
        if not self.check_limit or not prev_close or prev_close <= 0:
            return False
        chg = price / prev_close - 1.0
        lim = inst.limit_pct
        if side == "BUY" and chg >= lim - 0.002:
            return True
        if side == "SELL" and chg <= -lim + 0.002:
            return True
        return False

    # ---------- 买入 ----------

    def check_buy(self, code, price, trade_date, amount=None, target_pct=None,
                  prices=None, prev_close=None, name=""):
        """校验 BUY 并算好整手股数：amount 金额优先，否则按 target_pct 差额。"""
        inst = self.market.classify(code, name)
        if price <= 0:
            return RiskDecision(False, STATUS_NO_CHANGE, "无行情", instrument=inst)
        if code not in self.account.lots and \
                len(self.account.position_codes()) >= self.max_positions:
            return RiskDecision(False, STATUS_REJECTED, "持仓数已满",
                                instrument=inst)
        if self._limit_blocked(inst, "BUY", price, prev_close):
            return RiskDecision(False, STATUS_REJECTED, "涨停附近无法买入",
                                instrument=inst)

        prices = prices or {code: price}
        if amount is None:
            total = self.account.total_asset(prices)
            target_value = total * float(target_pct or 0) / 100.0
            current = self.account.position_value(code, price)
            amount = target_value - current
        # 单票上限
        total = self.account.total_asset(prices)
        cap = total * self.max_position_pct / 100.0
        current = self.account.position_value(code, price)
        amount = min(float(amount), cap - current)
        if amount < self.min_amount_for(total):
            return RiskDecision(False, STATUS_NO_CHANGE, "金额不足",
                                instrument=inst)
        lot = inst.lot_size
        shares = int(amount / price)
        shares -= shares % lot
        if shares <= 0:
            # 目标金额不足一手但单票额度够：按一手取整（小资金常见）
            lot_cost = price * lot
            if lot_cost <= cap - current + 1e-9:
                shares = lot
        shares = min(shares, self.account.max_buy_shares(
            price, transfer_fee_rate=(None if inst.transfer_fee else 0.0)))
        if shares <= 0:
            return RiskDecision(False, STATUS_NO_CHANGE, "现金不足",
                                instrument=inst)
        return RiskDecision(True, STATUS_VALIDATED, "", shares=shares,
                            amount=shares * price, instrument=inst)

    # ---------- 卖出 ----------

    def check_sell(self, code, price, trade_date, shares=None, prev_close=None,
                   name=""):
        """校验 SELL：默认清掉全部可卖份额。"""
        inst = self.market.classify(code, name)
        if price <= 0:
            return RiskDecision(False, STATUS_NO_CHANGE, "无行情", instrument=inst)
        avail = self.account.available_shares(code, trade_date)
        if avail <= 0:
            return RiskDecision(False, STATUS_NO_CHANGE, "无可卖份额(T+1)",
                                instrument=inst)
        if self._limit_blocked(inst, "SELL", price, prev_close):
            return RiskDecision(False, STATUS_REJECTED, "跌停附近无法卖出",
                                instrument=inst)
        shares = int(avail if shares is None else min(shares, avail))
        if shares <= 0:
            return RiskDecision(False, STATUS_NO_CHANGE, "无可卖份额",
                                instrument=inst)
        return RiskDecision(True, STATUS_VALIDATED, "", shares=shares,
                            amount=shares * price, instrument=inst)
