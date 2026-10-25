"""trading.multi_account: 多市场浮动份额账户 (fund/hk/us/crypto).

StockAccount 只支持 int 整手 (A股100股), fund 份额小数 / crypto 小数
会直接被 int() 截断为 0. 本账户支持 float 份额:

- lots: {code: [{shares(float), cost, buy_date, sellable_date}]}
- T+0 (hk/us/crypto) sellable_date=当日; T+1/fund=下一交易日 (调用方传入).
- 费用: 买入 佣金(max)+过户(可选); 卖出 +印花(可选). 各市场费率由调用方
  按 config/markets.yaml 传入, 默认与 StockAccount 一致.
- 精度: fund 保留 4 位, crypto 6 位, hk/us 保留 0 位 (整股), 内部统一 float.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FloatLot:
    code: str
    shares: float
    cost: float
    buy_date: str
    sellable_date: str
    reason: str = ""
    confidence: float = 0.0


@dataclass
class FloatTrade:
    timestamp: datetime
    code: str
    side: str
    price: float
    shares: float
    amount: float
    fee: float
    realized: float = 0.0
    reason: str = ""
    confidence: float = 0.0
    trade_date: str = ""
    sellable_date: str = ""

    def to_dict(self):
        return {
            "time": self.timestamp.isoformat(), "code": self.code,
            "side": self.side, "price": round(self.price, 6),
            "shares": round(self.shares, 6),
            "amount": round(self.amount, 2), "fee": round(self.fee, 2),
            "realized": round(self.realized, 2), "reason": self.reason,
            "confidence": self.confidence, "trade_date": self.trade_date,
            "sellable_date": self.sellable_date,
        }


class MultiAccount:
    def __init__(self, initial_capital, commission_rate=2.5e-4,
                 min_commission=5.0, stamp_tax_rate=5e-4,
                 transfer_fee_rate=1e-5):
        self.initial_capital = float(initial_capital)
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.transfer_fee_rate = float(transfer_fee_rate)
        self.cash = float(initial_capital)
        self.lots = {}
        self.last_prices = {}   # {code: 最近一次已知价} 停牌/跌出池时估值兜底
        self.fees = 0.0
        self.realized_pnl = 0.0
        self.trade_count = 0

    # ---------- 费用 ----------
    def _buy_fee(self, amount, transfer_fee_rate=None):
        commission = max(amount * self.commission_rate, self.min_commission)
        # 小额 crypto/fund 免最低佣金挤压: 金额<50 时按比例 (调用方可覆写 min_commission=0)
        transfer = amount * (self.transfer_fee_rate
                             if transfer_fee_rate is None
                             else float(transfer_fee_rate))
        return commission + transfer

    def _sell_fee(self, amount, stamp_tax_rate=None, transfer_fee_rate=None):
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp = amount * (self.stamp_tax_rate
                          if stamp_tax_rate is None
                          else float(stamp_tax_rate))
        transfer = amount * (self.transfer_fee_rate
                             if transfer_fee_rate is None
                             else float(transfer_fee_rate))
        return commission + stamp + transfer

    # ---------- 持仓 ----------
    def total_shares(self, code):
        return sum(l.shares for l in self.lots.get(code, []))

    def available_shares(self, code, trade_date):
        d = str(trade_date)
        return sum(l.shares for l in self.lots.get(code, [])
                   if l.sellable_date <= d)

    def position_codes(self):
        return [c for c, ls in self.lots.items()
                if sum(l.shares for l in ls) > 1e-12]

    def position_value(self, code, price):
        return self.total_shares(code) * (price or 0.0)

    def price_of(self, code, prices):
        """持仓估值价: 当日价优先, 缺失(停牌/跌出池)回退最近已知价, 仍缺为 0."""
        px = float((prices or {}).get(code) or 0.0)
        if px > 0:
            self.last_prices[code] = px
            return px
        return float(self.last_prices.get(code, 0.0))

    def market_value(self, prices):
        return sum(self.total_shares(c) * self.price_of(c, prices)
                   for c in self.position_codes())

    def total_asset(self, prices):
        return self.cash + self.market_value(prices)

    def position_pct(self, code, prices):
        total = self.total_asset(prices)
        if total <= 0:
            return 0.0
        return self.position_value(code, prices.get(code, 0.0)) / total * 100

    # ---------- 交易 ----------
    def buy(self, code, price, shares, trade_date, sellable_date,
            reason="", confidence=0.0, transfer_fee_rate=None):
        shares = float(shares)
        if shares <= 0 or price <= 0:
            raise ValueError(f"买入数量/价格非法: {shares}@{price}")
        amount = price * shares
        fee = self._buy_fee(amount, transfer_fee_rate)
        if amount + fee > self.cash + 1e-9:
            raise ValueError(f"现金不足: 需 {amount + fee:.2f}, 有 {self.cash:.2f}")
        self.cash -= amount + fee
        self.fees += fee
        self.trade_count += 1
        self.last_prices[code] = float(price)
        lot = FloatLot(code=code, shares=shares, cost=(amount + fee) / shares,
                       buy_date=str(trade_date),
                       sellable_date=str(sellable_date),
                       reason=reason, confidence=confidence)
        self.lots.setdefault(code, []).append(lot)
        return FloatTrade(datetime.now(), code, "BUY", price, shares,
                          amount, fee, 0.0, reason, confidence,
                          str(trade_date), str(sellable_date))

    def sell(self, code, price, shares, trade_date, reason="",
             confidence=0.0, stamp_tax_rate=None, transfer_fee_rate=None):
        shares = float(shares)
        avail = self.available_shares(code, trade_date)
        if shares <= 0:
            raise ValueError("卖出数量必须大于0")
        if shares - avail > 1e-9:
            raise ValueError(f"可卖 {avail:.6f}, 不足 {shares:.6f}")
        shares = min(shares, avail)
        amount = price * shares
        fee = self._sell_fee(amount, stamp_tax_rate, transfer_fee_rate)
        remaining, realized, keep = shares, 0.0, []
        for lot in sorted(self.lots.get(code, []),
                          key=lambda x: x.sellable_date):
            if remaining <= 1e-12:
                keep.append(lot)
                continue
            if lot.sellable_date > str(trade_date):
                keep.append(lot)
                continue
            take = min(lot.shares, remaining)
            share_fee = fee * (take / shares) if shares else 0.0
            realized += take * price - share_fee - lot.cost * take
            lot.shares -= take
            remaining -= take
            if lot.shares > 1e-12:
                keep.append(lot)
        self.lots[code] = keep
        if not keep:
            self.lots.pop(code, None)
        self.last_prices[code] = float(price)
        self.cash += amount - fee
        self.fees += fee
        self.realized_pnl += realized
        self.trade_count += 1
        return FloatTrade(datetime.now(), code, "SELL", price, shares,
                          amount, fee, realized, reason, confidence,
                          str(trade_date))

    # ---------- 持久化/快照 ----------
    def to_state(self):
        return {
            "initial_capital": self.initial_capital,
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "cash": self.cash, "fees": self.fees,
            "last_prices": dict(self.last_prices),
            "realized_pnl": self.realized_pnl,
            "trade_count": self.trade_count,
            "lots": {c: [vars(l) for l in ls] for c, ls in self.lots.items()},
        }

    @classmethod
    def from_state(cls, s):
        a = cls(s.get("initial_capital", 0),
                s.get("commission_rate", 2.5e-4),
                s.get("min_commission", 5.0),
                s.get("stamp_tax_rate", 5e-4),
                s.get("transfer_fee_rate", 1e-5))
        a.cash = s.get("cash", a.initial_capital)
        a.last_prices = dict(s.get("last_prices") or {})
        a.fees = s.get("fees", 0.0)
        a.realized_pnl = s.get("realized_pnl", 0.0)
        a.trade_count = s.get("trade_count", 0)
        a.lots = {c: [FloatLot(**l) for l in ls]
                  for c, ls in s.get("lots", {}).items()}
        return a

    def snapshot(self, prices):
        pos = {}
        for code in self.position_codes():
            sh = self.total_shares(code)
            px = self.price_of(code, prices)
            cost = (sum(l.cost * l.shares for l in self.lots[code]) / sh
                    if sh else 0.0)
            pos[code] = {
                "shares": round(sh, 6),
                "pct": round(self.position_pct(code, prices), 2),
                "cost": round(cost, 6), "price": round(px, 6),
                "pnl_pct": round((px / cost - 1) * 100, 2) if cost else 0.0,
            }
        mv = self.market_value(prices)
        return {"cash": round(self.cash, 2), "fees": round(self.fees, 2),
                "realized_pnl": round(self.realized_pnl, 2),
                "unrealized_pnl": round(mv - sum(
                    l.cost * l.shares for ls in self.lots.values()
                    for l in ls), 2),
                "total_asset": round(self.cash + mv, 2),
                "trade_count": self.trade_count, "positions": pos}
