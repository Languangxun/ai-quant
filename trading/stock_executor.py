"""股票/ETF 执行器：订单 → 风控拦截 → A股整手成交。

分层（借鉴 akquant 的 MarketModel / RiskManager / 订单事件流）：
- trading.instruments.MarketModel：标的规则（T+1/T+0、涨跌停、费用）
- risk.stock_rules.StockRiskManager：预交易校验与下单量计算
- trading.orders.OrderJournal：订单状态流转记录
本类只做编排：取价、调风控、写账户、记日志，规则本身不在这里。
"""
from datetime import date, datetime

from trading.calendar import TradingCalendar
from trading.instruments import MarketModel, price_limit_pct  # noqa: F401
from trading.orders import (OrderJournal, STATUS_FILLED, STATUS_HOLD,
                            STATUS_NO_CHANGE, STATUS_REJECTED)
from risk.stock_rules import StockRiskManager

__all__ = ["StockExecutor", "price_limit_pct"]


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


class StockExecutor:
    def __init__(self, account, calendar=None, max_positions=5,
                 max_position_pct=20.0, min_order_amount=5000.0,
                 min_order_pct=5.0, check_limit=True, market=None,
                 journal=None):
        self.account = account
        self.calendar = calendar or TradingCalendar()
        self.market = market or MarketModel()
        self.journal = journal if journal is not None else OrderJournal()
        self.risk = StockRiskManager(
            account, market=self.market, max_positions=max_positions,
            max_position_pct=max_position_pct,
            min_order_amount=min_order_amount, min_order_pct=min_order_pct,
            check_limit=check_limit)
        self.max_positions = self.risk.max_positions
        self.max_position_pct = self.risk.max_position_pct
        self.min_order_amount = self.risk.min_order_amount
        self.min_order_pct = self.risk.min_order_pct
        self.check_limit = check_limit
        self.history = []

    def min_amount_for(self, total_asset):
        """当前总资产下的单笔最小买入金额（固定上限与比例下限取小）。"""
        return self.risk.min_amount_for(total_asset)

    def _next_date(self, trade_date):
        return str(self.calendar.next_trading_day(_as_date(trade_date)))

    def _sellable_date(self, trade_date, inst):
        return str(trade_date) if inst.t0 else self._next_date(trade_date)

    # ---------- 订单生命周期 ----------

    def _record(self, code, action, status, inst=None, session=None,
                reason="", price=None, shares=0, amount=0.0, fee=None,
                realized=None, position=None, confidence=None):
        rec = {
            "code": str(code or "").lower(),
            "action": str(action or "HOLD").upper(),
            "status": status,
            "reason": reason or "",
            "session": session,
        }
        if inst is not None:
            rec.update({"name": inst.name, "kind": inst.kind,
                        "t0": inst.t0, "limit_pct": inst.limit_pct})
        if price is not None:
            rec["price"] = round(float(price), 4)
        if shares:
            rec["shares"] = int(shares)
        if amount:
            rec["amount"] = round(float(amount), 2)
        if fee is not None:
            rec["fee"] = round(float(fee), 2)
        if realized is not None:
            rec["realized"] = round(float(realized), 2)
        if position is not None:
            rec["position"] = position
        if confidence is not None:
            rec["confidence"] = confidence
        return self.journal.add(**rec)

    def _apply(self, action, code, price, trade_date, decision, reason="",
               confidence=0.0, session=None):
        inst = decision.instrument
        if not decision.allowed:
            label = "REJECT" if decision.status == STATUS_REJECTED \
                else "NO CHANGE"
            self._record(code, action, decision.status, inst=inst,
                         session=session, reason=decision.reason,
                         price=price, confidence=confidence)
            return f"{label} {decision.reason}"
        try:
            if action == "BUY":
                trade = self.account.buy(
                    code, price, decision.shares, trade_date,
                    self._sellable_date(trade_date, inst),
                    reason=reason, confidence=confidence,
                    transfer_fee_rate=(None if inst.transfer_fee else 0.0))
            else:
                trade = self.account.sell(
                    code, price, decision.shares, trade_date,
                    reason=reason, confidence=confidence,
                    stamp_tax_rate=(None if inst.stamp_tax else 0.0),
                    transfer_fee_rate=(None if inst.transfer_fee else 0.0))
        except ValueError as e:
            self._record(code, action, STATUS_REJECTED, inst=inst,
                         session=session, reason=str(e), price=price,
                         confidence=confidence)
            return f"REJECT {e}"
        self.history.append(trade)
        self._record(code, action, STATUS_FILLED, inst=inst, session=session,
                     reason=reason, price=price, shares=trade.shares,
                     amount=trade.amount, fee=trade.fee,
                     realized=trade.realized, confidence=confidence)
        return trade

    # ---------- 公开下单接口 ----------

    def buy(self, code, price, trade_date, amount=None, target_pct=None,
            reason="", confidence=0.0, prices=None, prev_close=None, name=""):
        """买入：amount 金额优先，否则按 target_pct 目标仓位计算差额。"""
        decision = self.risk.check_buy(
            code, price, trade_date, amount=amount, target_pct=target_pct,
            prices=prices, prev_close=prev_close, name=name)
        return self._apply("BUY", code, price, trade_date, decision,
                           reason=reason, confidence=confidence)

    def sell(self, code, price, trade_date, shares=None, reason="",
             confidence=0.0, prev_close=None, name=""):
        """卖出：默认清掉全部可卖份额。"""
        decision = self.risk.check_sell(
            code, price, trade_date, shares=shares, prev_close=prev_close,
            name=name)
        return self._apply("SELL", code, price, trade_date, decision,
                           reason=reason, confidence=confidence)

    def execute_order(self, order, prices, trade_date, prev_closes=None,
                      names=None, session=None):
        """执行 LLM 订单：{"code","action","position","reason","confidence"}。"""
        code = str(order.get("code") or "").lower()
        action = str(order.get("action") or "HOLD").upper()
        name = (names or {}).get(code) or order.get("name") or ""
        try:
            confidence = float(order.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = order.get("reason", "")
        if not code or action == "HOLD":
            self._record(code, action or "HOLD", STATUS_HOLD, session=session,
                         reason=reason, confidence=confidence)
            return "HOLD"
        price = prices.get(code)
        prev_close = (prev_closes or {}).get(code)
        if not price or price <= 0:
            self._record(code, action, STATUS_NO_CHANGE, session=session,
                         reason="无行情", confidence=confidence)
            return "NO CHANGE 无行情"
        if action == "BUY":
            pos = order.get("position")
            try:
                target_pct = None if pos is None else float(pos)
            except (TypeError, ValueError):
                target_pct = None
            decision = self.risk.check_buy(
                code, price, trade_date, target_pct=target_pct, prices=prices,
                prev_close=prev_close, name=name)
        elif action == "SELL":
            decision = self.risk.check_sell(
                code, price, trade_date, prev_close=prev_close, name=name)
        else:
            self._record(code, action, STATUS_REJECTED, session=session,
                         reason=f"非法动作 {action}", price=price,
                         confidence=confidence)
            return f"REJECT 非法动作 {action}"
        return self._apply(action, code, price, trade_date, decision,
                           reason=reason, confidence=confidence,
                           session=session)

    def mark(self, prices):
        """估值快照。"""
        return self.account.snapshot(prices)
