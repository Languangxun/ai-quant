"""股票组合决策层：deepseek-v4.1-flash（deepseek-flash）+ qwen3-embedding 记忆。

- 候选池：stock_predict CLI 多维评分选股（data.stock.cli_bridge.candidates）
- 记忆：qwen3-embedding 向量检索相似历史（memory.otc_memory）
- 输出：orders 列表（BUY/SELL + 目标仓位），交由 trading.stock_executor 执行
- API 余额不足/额度耗尽/鉴权失败时自动降级为纯代码评分轮动
"""
from models.deepseek import DeepSeekModel, is_network_error, is_quota_error
from models.stock_prompt import build_system_prompt

DEFAULT_MAX_POSITIONS = 5
DEFAULT_MAX_POSITION_PCT = 20.0
DEFAULT_STOP_LOSS_PCT = -5.0


def _code(code):
    return str(code or "").lower()


def code_only_decision(context, reason=""):
    ctx = context or {}
    candidates = []
    for c in ctx.get("candidates") or []:
        try:
            score = float(c.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if c.get("code") and score > 0:
            candidates.append((c, score))
    positions = [p for p in (ctx.get("positions") or []) if p.get("code")]
    account = ctx.get("account") or {}
    try:
        max_positions = max(1, int(
            account.get("max_positions") or DEFAULT_MAX_POSITIONS))
    except (TypeError, ValueError):
        max_positions = DEFAULT_MAX_POSITIONS
    try:
        max_pct = float(
            account.get("max_position_pct") or DEFAULT_MAX_POSITION_PCT)
    except (TypeError, ValueError):
        max_pct = DEFAULT_MAX_POSITION_PCT
    bought = {_code(c) for c in (account.get("bought_today") or [])}
    sold = {_code(c) for c in (account.get("sold_today") or [])}
    held = {_code(p.get("code")) for p in positions}
    rank = {_code(c.get("code")): i for i, (c, _) in enumerate(candidates)}
    source = f"（{str(reason)[:80]}）" if reason else ""
    view = (f"LLM不可用，纯代码降级：评分轮动、持仓≤{max_positions}、"
            f"单票≤{max_pct:.0f}%{source}")

    if not candidates:
        return {"market_view": view + "；无有效候选，观望",
                "orders": [], "degraded": True,
                "degrade_reason": str(reason or "")}

    orders = []
    for p in positions:
        code = _code(p.get("code"))
        if int(p.get("sellable") or 0) <= 0:
            continue
        try:
            pnl = float(p.get("pnl_pct") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        stop = pnl <= DEFAULT_STOP_LOSS_PCT
        weak = rank.get(code, max_positions) >= max_positions
        if not (stop or weak):
            continue
        why = "跌破止损" if stop else "跌出评分前列"
        orders.append({
            "code": code,
            "action": "SELL",
            "reason": f"代码降级:{why}",
            "confidence": 0.6,
        })

    sell_codes = {o["code"] for o in orders if o["action"] == "SELL"}
    slots = max(0, max_positions - (len(held) - len(sell_codes)))
    target_pct = min(max_pct, 100.0 / max_positions)
    for c, score in candidates:
        if slots <= 0:
            break
        code = _code(c.get("code"))
        if code in held or code in bought or code in sold:
            continue
        orders.append({
            "code": code,
            "action": "BUY",
            "position": round(target_pct, 2),
            "reason": f"代码降级:候选评分{score:g}",
            "confidence": round(min(0.9, max(0.5, score / 100.0)), 2),
        })
        slots -= 1

    return {"market_view": view, "orders": orders, "degraded": True,
            "degrade_reason": str(reason or "")}


class StockDecisionMaker:
    def __init__(self, system_prompt=None):
        self.client = None
        self.model = None
        self.init_error = None
        try:
            self.client = DeepSeekModel(
                system_prompt=system_prompt or build_system_prompt())
            self.model = self.client.model
        except Exception as e:
            self.init_error = e

    def decide(self, context):
        """返回 (result_dict, error)。result 含 market_view / orders。"""
        if self.client is None:
            return code_only_decision(context, self.init_error), None
        try:
            result = self.client.analyze(context)
        except Exception as e:
            if is_quota_error(e):
                return code_only_decision(context, e), None
            if is_network_error(e):
                return code_only_decision(context, e), None
            return None, str(e)
        if not isinstance(result, dict):
            return None, f"模型输出非 JSON 对象: {str(result)[:120]}"
        orders = result.get("orders")
        if orders is None:
            result["orders"] = []
        elif not isinstance(orders, list):
            return None, "orders 字段不是列表"
        return result, None
