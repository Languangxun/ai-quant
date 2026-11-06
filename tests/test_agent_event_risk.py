"""事件风险与订单覆盖逻辑测试（纯代码，无网络/LLM 请求）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import event_risk


def _item(title, summary=""):
    return {"title": title, "summary": summary}


def test_normalize_clamps_and_defaults():
    r = event_risk.normalize({
        "level": "HIGH",
        "posture": "defensive",
        "risk_score": 99,
        "events": ["加征关税", {"name": "停火", "direction": "positive"}],
        "beneficiaries": ["黄金", "黄金", ""],
        "confidence": 2,
    })
    assert r["level"] == "high"
    assert r["posture"] == "defensive"
    assert r["risk_score"] == 5
    assert r["confidence"] == 1
    assert [e["name"] for e in r["events"]] == ["加征关税", "停火"]
    assert r["beneficiaries"] == ["黄金"]


def test_heuristic_tariff_escalation_is_high_defensive():
    text = ("美国宣布对华加征关税，贸易战升级，制裁与断供扩大；"
            "全球股市暴跌，避险升温，衰退担忧加剧")
    r = event_risk.heuristic([_item(text, "战争 冲突 加息")],
                             "创业板在MA20下方，市场偏弱")
    assert r["level"] == "high"
    assert r["posture"] == "defensive"
    assert r["risk_score"] < 0
    assert any("出口" in x for x in r["hurt"])
    assert r["actions"]


def test_heuristic_mixed_event_caps_at_elevated():
    items = [
        _item("特朗普关税升级，出口链承压，市场避险",
              "民主党中期选举大胜，关税谈判与豁免预期升温，"
              "停火谈判推进，风险偏好回升"),
        _item("美国中期选举后政策不确定性上升", "关税协议仍有变数"),
    ]
    r = event_risk.heuristic(items, "上证在MA20下方，市场偏弱")
    assert r["level"] in ("normal", "elevated")
    assert r["level"] != "high"


def test_heuristic_positive_event_but_weak_market_not_offensive():
    items = [_item("民主党大胜中期选举，关税升级概率下降，"
                   "不确定性出清，风险偏好回升",
                   "美伊局势降级，停火谈判推进，市场反弹")]
    r = event_risk.heuristic(items, "上证在MA20下方，市场偏弱")
    assert r["risk_score"] > 0
    assert r["posture"] != "offensive"
    assert r["posture"] in ("neutral", "defensive")


def test_merge_llm_overrides_only_present_fields():
    fallback = event_risk.normalize({
        "level": "elevated", "risk_score": -2,
        "hurt": ["出口链"], "actions": ["控制仓位"],
    })
    got = event_risk.merge(
        {"level": "high", "summary": "关税升级", "confidence": 0.8},
        fallback)
    assert got["level"] == "high"
    assert got["summary"] == "关税升级"
    assert got["confidence"] == 0.8
    assert got["hurt"] == ["出口链"]
    assert got["risk_score"] == -2
    assert got["actions"] == ["控制仓位"]


def test_merge_conservative_cannot_downgrade():
    fallback = event_risk.normalize({
        "level": "high", "posture": "defensive", "risk_score": -4,
        "hurt": ["出口链"], "actions": ["保留现金"],
    })
    got = event_risk.merge(
        {"level": "normal", "posture": "offensive", "risk_score": 2,
         "summary": "利空出尽", "beneficiaries": ["出口链"]},
        fallback, conservative=True)
    assert got["level"] == "high"
    assert got["posture"] == "defensive"
    assert got["risk_score"] == -4
    assert "出口链" in got["hurt"] and "出口链" in got["beneficiaries"]
    assert "保留现金" in got["actions"]


def test_merge_conservative_can_escalate():
    fallback = event_risk.normalize({
        "level": "normal", "posture": "neutral", "risk_score": 0})
    got = event_risk.merge(
        {"level": "high", "posture": "defensive", "risk_score": -5,
         "summary": "关税升级"}, fallback, conservative=True)
    assert got["level"] == "high"
    assert got["posture"] == "defensive"
    assert got["risk_score"] == -5
    assert got["summary"] == "关税升级"


def _ctx():
    return {
        "candidates": [
            {"code": "sz000001", "name": "平安银行", "industry": "银行"},
            {"code": "sh600547", "name": "山东黄金", "industry": "黄金"},
            {"code": "sz002050", "name": "三花智控",
             "industry": "汽车零部件"},
        ]
    }


def test_overlay_normal_is_noop():
    orders = [{"code": "sz000001", "action": "BUY", "position": 20}]
    out, notes = event_risk.overlay_orders(
        orders, _ctx(), event_risk.default(), {})
    assert out == orders and notes == []


def test_overlay_high_caps_and_prefers_beneficiary():
    risk = {"level": "high", "posture": "defensive", "risk_score": -4,
            "hurt": ["汽车零部件"], "beneficiaries": ["黄金"]}
    orders = [
        {"code": "sz000001", "action": "BUY", "position": 20},
        {"code": "sh600547", "action": "BUY", "position": 20},
        {"code": "sz002050", "action": "BUY", "position": 10},
        {"code": "sh600000", "action": "SELL"},
    ]
    out, notes = event_risk.overlay_orders(orders, _ctx(), risk, {})
    codes = [o["code"] for o in out]
    assert "sh600000" in codes and "sz002050" not in codes
    buys = [o for o in out if o["action"] == "BUY"]
    assert [o["code"] for o in buys] == ["sh600547"]
    assert buys[0]["position"] == 10
    assert any("受益" in n for n in notes)
    assert any("受损" in n for n in notes)


def test_overlay_elevated_caps_positions_and_count():
    risk = {"level": "elevated", "posture": "neutral", "risk_score": -2}
    orders = [
        {"code": "sz000001", "action": "BUY", "position": 20},
        {"code": "sh600547", "action": "BUY", "position": 18},
        {"code": "sz002050", "action": "BUY", "position": 12},
    ]
    out, notes = event_risk.overlay_orders(orders, _ctx(), risk, {
        "elevated_max_position_pct": 15, "elevated_max_buys": 2})
    assert len(out) == 2
    assert all(o["position"] <= 15 for o in out)
    assert any("上限" in n for n in notes)


def test_overlay_elevated_defensive_does_not_block_hurt():
    risk = {"level": "elevated", "posture": "defensive", "risk_score": -3,
            "hurt": ["汽车零部件"]}
    orders = [
        {"code": "sz002050", "action": "BUY", "position": 20},
        {"code": "sz000001", "action": "BUY", "position": 12},
    ]
    out, notes = event_risk.overlay_orders(orders, _ctx(), risk, {
        "elevated_max_position_pct": 15, "elevated_max_buys": 2,
        "high_max_position_pct": 10, "high_max_buys": 1,
    })
    assert [o["code"] for o in out] == ["sz002050", "sz000001"]
    assert all(o["position"] <= 15 for o in out)
    assert not any("受损" in n for n in notes)


def test_overlay_disabled():
    risk = {"level": "high", "posture": "defensive", "risk_score": -5}
    orders = [{"code": "sz000001", "action": "BUY", "position": 20}]
    out, notes = event_risk.overlay_orders(
        orders, _ctx(), risk, {"enabled": False})
    assert out == orders and notes == []


def test_overlay_matches_parenthetical_and_conjunction_sectors():
    ctx = {"candidates": [
        {"code": "sh600519", "name": "贵州茅台", "industry": "白酒"},
        {"code": "sz300760", "name": "迈瑞医疗", "industry": "创新药"},
        {"code": "sz002371", "name": "北方华创", "industry": "半导体"},
    ]}
    risk = {"level": "high", "posture": "defensive", "risk_score": -4,
            "hurt": ["半导体（存储/光模块）"],
            "beneficiaries": ["创新药与出口链"]}
    orders = [
        {"code": "sh600519", "action": "BUY", "position": 8},
        {"code": "sz002371", "action": "BUY", "position": 8},
        {"code": "sz300760", "action": "BUY", "position": 8},
    ]
    out, notes = event_risk.overlay_orders(orders, ctx, risk, {})
    codes = [o["code"] for o in out]
    assert "sz002371" not in codes
    assert codes[0] == "sz300760"  # 受益方向优先于中性标的
    assert any("半导体" in n for n in notes)
    assert any("创新药" in n for n in notes)
