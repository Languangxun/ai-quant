"""事件风险：特朗普/关税/地缘/美联储等外部冲击的 A 股传导。

盘前 LLM 会输出结构化 event_risk；解析失败时用关键词规则兜底。
决策层把 event_risk 当作“风险姿态”，不是交易指令：
- high / defensive：收紧单票上限、限制当日新增买入、跳过明确受损板块
- elevated：温和收紧，仍以候选评分与行情为主
- normal / offensive：不额外限制

所有阈值在 config/stock.yaml -> stock.event_risk，可关闭或调整。
"""
import re

LEVELS = ("normal", "elevated", "high")
POSTURES = ("offensive", "neutral", "defensive")

_NEG = (
    ("加征关税", 2.0), ("关税升级", 2.0), ("贸易战", 2.0),
    ("制裁", 2.0), ("实体清单", 2.0), ("断供", 2.0),
    ("战争", 2.0), ("空袭", 2.0), ("封锁", 2.0), ("冲突", 1.2),
    ("衰退", 1.5), ("暴跌", 1.5), ("通胀", 1.0), ("鹰派", 1.0),
    ("加息", 1.5), ("risk-off", 2.0), ("避险", 1.0),
    # 英文原始报道（中英文交叉检索时避免漏判）
    ("tariff hike", 2.0), ("new tariff", 1.8), ("trade war", 2.0),
    ("sanction", 1.8), ("entity list", 1.8), ("rate hike", 1.5),
    ("hawkish", 1.0), ("recession", 1.5), ("sell-off", 1.5),
    ("war", 1.5), ("conflict", 1.0),
)
_POS = (
    ("谈判", 1.5), ("协议", 1.5), ("停火", 2.0), ("降级", 1.5),
    ("豁免", 1.5), ("推迟", 1.0), ("降息", 1.5), ("宽松", 1.0),
    ("反弹", 1.0), ("大涨", 1.0), ("修复", 1.0), ("缓和", 1.5),
    ("不确定性出清", 2.0), ("风险偏好回升", 1.5),
    ("tariff cut", 1.5), ("tariff exemption", 1.5), ("trade deal", 1.5),
    ("ceasefire", 2.0), ("rate cut", 1.5), ("dovish", 1.0),
    ("truce", 1.5),
)

_HURT_TARIFF = ("出口链", "消费电子", "光伏", "家电", "纺织", "航运",
                "汽车零部件", "半导体", "机械", "化工")
_BENEFIT_TARIFF = ("黄金", "贵金属", "军工", "农业", "内需", "消费",
                   "国产替代", "稀土", "创新药")
_BENEFIT_EASING = ("出口链", "消费电子", "航运", "家电", "汽车零部件",
                   "创新药", "光伏")
_BENEFIT_FED_CUT = ("黄金", "贵金属", "成长科技", "创新药", "券商")
_HURT_FED_CUT = ("银行", "保险")
_BENEFIT_OIL_UP = ("油气", "煤炭", "油服")
_HURT_OIL_UP = ("航空", "化工下游", "汽车", "物流")
_BENEFIT_OIL_DOWN = ("航空", "化工下游", "汽车", "物流")
_HURT_OIL_DOWN = ("油气", "煤炭", "油服")


def default():
    return {
        "level": "normal",
        "posture": "neutral",
        "risk_score": 0.0,
        "summary": "",
        "events": [],
        "beneficiaries": [],
        "hurt": [],
        "actions": [],
        "confidence": 0.0,
    }


def _clamp(value, lo, hi):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return lo


def _strings(value, limit=8, width=40):
    out = []
    for x in value or []:
        s = re.sub(r"\s+", " ", str(x or "")).strip()
        if s and s not in out:
            out.append(s[:width])
        if len(out) >= limit:
            break
    return out


def normalize(raw):
    """校验/裁剪 LLM 或规则输出，永远返回完整结构。"""
    out = default()
    if not isinstance(raw, dict):
        return out
    level = str(raw.get("level") or "").strip().lower()
    if level in LEVELS:
        out["level"] = level
    posture = str(raw.get("posture") or "").strip().lower()
    if posture in POSTURES:
        out["posture"] = posture
    out["risk_score"] = round(_clamp(raw.get("risk_score"), -5, 5), 1)
    out["summary"] = re.sub(
        r"\s+", " ", str(raw.get("summary") or "")).strip()[:300]
    events = []
    for ev in raw.get("events") or []:
        if isinstance(ev, str):
            ev = {"name": ev}
        if not isinstance(ev, dict):
            continue
        name = re.sub(r"\s+", " ", str(ev.get("name") or "")).strip()[:60]
        if not name:
            continue
        direction = str(ev.get("direction") or "").strip().lower()
        if direction not in ("positive", "negative", "mixed", "neutral"):
            direction = "mixed"
        events.append({
            "name": name,
            "direction": direction,
            "impact": re.sub(
                r"\s+", " ", str(ev.get("impact") or "")).strip()[:160],
        })
        if len(events) >= 5:
            break
    out["events"] = events
    out["beneficiaries"] = _strings(raw.get("beneficiaries"))
    out["hurt"] = _strings(raw.get("hurt"))
    out["actions"] = _strings(raw.get("actions"), limit=6, width=60)
    out["confidence"] = round(_clamp(raw.get("confidence"), 0, 1), 2)
    return out


def _count(text, words):
    score = 0.0
    hits = []
    for word, weight in words:
        n = text.count(word)
        if n:
            score += weight * min(n, 3)
            hits.append((word, n))
    return score, hits


def heuristic(items, market_brief="", predictions=None):
    """关键词兜底：LLM JSON 解析失败/未启用时仍能给出风险姿态。"""
    text = " ".join(
        f"{it.get('title') or ''} {it.get('summary') or ''}"
        for it in (items or []) if isinstance(it, dict))
    text = text.lower()
    neg, neg_hits = _count(text, _NEG)
    pos, pos_hits = _count(text, _POS)
    score = max(-5.0, min(5.0, pos - neg))
    # 多空双方都有较多命中时，说明事件方向未定（如“加沙停火”与
    # “关税升级”同时出现），按 elevated 处理，不直接进入 high 的
    # 极端收紧；真正一边倒的重大升级仍会落到 high。
    mixed = pos >= 3 and neg >= 3
    level = "normal"
    if neg >= 4 and pos < 2 and score <= -2:
        level = "high"
    elif abs(score) >= 2 or neg >= 2:
        level = "elevated"
    mb = str(market_brief or "")
    weak_market = ("偏弱" in mb) or ("下方" in mb and "上方" not in mb)
    strong_market = "偏强" in mb
    # 弱势市里只有“明显偏空且非多空交织”才升到 high；
    # 避免把“战争风险下降/停火”这类标题误判成重大利空。
    if weak_market and score <= -2 and neg >= 4 and not mixed:
        level = "high"
    posture = "neutral"
    if score >= 2:
        posture = "offensive"
    elif score <= -2:
        posture = "defensive"
    if weak_market:
        # 事件偏多但指数在均线下方：等价格确认，不因消息转进攻
        if posture == "offensive":
            posture = "neutral"
        if score <= 0:
            posture = "defensive"
    elif strong_market and score >= 1:
        posture = "offensive"

    beneficiaries, hurt, actions = [], [], []
    if any(w in text for w in ("关税", "贸易战", "制裁", "出口")):
        if score < 0:
            hurt += list(_HURT_TARIFF)
            beneficiaries += list(_BENEFIT_TARIFF)
        else:
            beneficiaries += list(_BENEFIT_EASING)
    if any(w in text for w in ("降息", "宽松", "美债收益率回落")):
        beneficiaries += list(_BENEFIT_FED_CUT)
        if score <= 0:
            hurt += list(_HURT_FED_CUT)
    if any(w in text for w in ("原油暴涨", "油价大涨", "供应中断")):
        beneficiaries += list(_BENEFIT_OIL_UP)
        hurt += list(_HURT_OIL_UP)
    elif any(w in text for w in ("原油暴跌", "油价大跌", "原油大跌")):
        beneficiaries += list(_BENEFIT_OIL_DOWN)
        hurt += list(_HURT_OIL_DOWN)
    if posture == "defensive":
        actions += ["控制单票仓位，不追高", "优先内需/黄金/军工等防御方向",
                    "事件未明朗前保留现金"]
    elif posture == "offensive":
        actions += ["可适度参与出口链/消费电子等修复",
                    "仍以实时行情与评分确认，不追单一消息"]
    else:
        actions += ["按评分与行情执行，事件仅作参考"]

    events = []
    for word, n in (neg_hits + pos_hits)[:4]:
        events.append({
            "name": word,
            "direction": "negative" if (word, n) in neg_hits else "positive",
            "impact": f"新闻中出现 {n} 次",
        })
    return normalize({
        "level": level,
        "posture": posture,
        "risk_score": score,
        "summary": (f"规则判断：外部事件{'偏空' if score < 0 else '偏多' if score > 0 else '中性'}"
                    f"（正{pos:.1f}/负{neg:.1f}）；"
                    f"市场简报{'偏弱' if weak_market else '非强势'}。"),
        "events": events,
        "beneficiaries": beneficiaries,
        "hurt": hurt,
        "actions": actions,
        "confidence": 0.35,
    })


_LEVEL_RANK = {"normal": 0, "elevated": 1, "high": 2}
_POSTURE_RANK = {"offensive": 0, "neutral": 1, "defensive": 2}


def merge(llm, fallback, conservative=False):
    """LLM 结果优先，缺失字段用规则结果补齐。

    conservative=True 用于决策期：模型可以升级风险，但不能把盘前已有的
    elevated/high 降级；风险分取更负值，受损/受益行业取并集。这样实时
    新闻只能加强保护，不能绕过确定性风控。
    """
    base = normalize(fallback)
    if not isinstance(llm, dict):
        return base
    got = normalize(llm)
    if conservative:
        if _LEVEL_RANK[got["level"]] >= _LEVEL_RANK[base["level"]]:
            base["level"] = got["level"]
        if _POSTURE_RANK[got["posture"]] >= _POSTURE_RANK[base["posture"]]:
            base["posture"] = got["posture"]
        base["risk_score"] = min(base["risk_score"], got["risk_score"])
        base["hurt"] = _strings(
            list(got["hurt"]) + list(base["hurt"]))
        base["beneficiaries"] = _strings(
            list(got["beneficiaries"]) + list(base["beneficiaries"]))
        base["actions"] = _strings(
            list(got["actions"]) + list(base["actions"]), limit=8)
        base["events"] = got["events"] or base["events"]
        base["confidence"] = max(base["confidence"], got["confidence"])
        if got["summary"] and (_LEVEL_RANK[got["level"]]
                               >= _LEVEL_RANK[base["level"]]):
            base["summary"] = got["summary"]
        return base
    for key in ("level", "posture", "summary", "beneficiaries", "hurt",
                "actions", "events", "confidence"):
        if key not in llm:
            continue
        value = got.get(key)
        if value not in (None, "", []):
            base[key] = value
    if "risk_score" in llm:  # 0 分是合法值
        base["risk_score"] = got["risk_score"]
    return base


def _sector_text(code, context):
    code = str(code or "").lower()
    for c in (context or {}).get("candidates") or []:
        if str(c.get("code") or "").lower() == code:
            return f"{c.get('name') or ''} {c.get('industry') or ''}".lower()
    for p in (context or {}).get("positions") or []:
        if str(p.get("code") or "").lower() == code:
            return f"{p.get('name') or ''} {p.get('industry') or ''}".lower()
    return ""


def _label_variants(label):
    """'半导体（存储/光模块）' -> 半导体；'创新药与出口链' -> 两段。"""
    raw = str(label or "").strip()
    if not raw:
        return []
    core = re.sub(r"[（(][^)）]*[)）]", "", raw).strip()
    parts = [raw, core]
    for piece in re.split(r"[、,，/;；与及\s]+", core):
        if len(piece) >= 2:
            parts.append(piece)
    out = []
    for p in parts:
        p = p.strip().lower()
        if p and p not in out:
            out.append(p)
    return out


def _hit_sector(text, labels):
    if not text:
        return ""
    for label in labels or []:
        for key in _label_variants(label):
            if key in text:
                return label
    return ""


def overlay_orders(orders, context, risk, cfg=None):
    """按事件风险收紧订单。返回 (new_orders, notes)。

    只收紧 BUY，不干预 SELL；level=normal/offensive 时基本不动作。
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return list(orders or []), []
    risk = normalize(risk)
    level, posture = risk["level"], risk["posture"]
    score = risk["risk_score"]
    if level == "normal":
        return list(orders or []), []
    if level == "elevated" and posture == "offensive" and score >= 2:
        return list(orders or []), []
    if level == "elevated":
        # elevated 只收紧仓位与笔数，不因“可能受损”直接封杀行业；
        # 是否跳过受损方向留给 high（或模型/人工判断）。
        cap = cfg.get("elevated_max_position_pct", 15)
        max_buys = cfg.get("elevated_max_buys", 2)
        block = False
        tag = "事件风险 elevated"
    else:
        cap = cfg.get("high_max_position_pct", 10)
        max_buys = cfg.get("high_max_buys", 1)
        block = bool(cfg.get("block_hurt_sectors", True))
        tag = "事件风险 high"

    try:
        cap = float(cap)
        max_buys = int(max_buys)
    except (TypeError, ValueError):
        return list(orders or []), []

    # SELL/HOLD 先保留；BUY 按“受益 > 中性 > 受损”排序后限额，
    # 避免事件高发时有限的买入名额被第一个中性标的占用。
    out, notes, buys = [], [], 0
    buy_rows = []
    for idx, order in enumerate(orders or []):
        o = dict(order or {})
        action = str(o.get("action") or "HOLD").upper()
        if action != "BUY":
            out.append(o)
            continue
        code = str(o.get("code") or "")
        text = _sector_text(code, context)
        hurt = _hit_sector(text, risk.get("hurt")) if block else ""
        benefit = _hit_sector(text, risk.get("beneficiaries"))
        buy_rows.append((1 if hurt else 0, 0 if benefit else 1, idx,
                         o, hurt, benefit))
    buy_rows.sort(key=lambda x: (x[0], x[1], x[2]))
    for _, _, _, o, hurt, benefit in buy_rows:
        code = str(o.get("code") or "")
        if hurt:
            notes.append(f"{code} 属事件受损方向「{hurt}」，跳过买入")
            continue
        if buys >= max_buys:
            notes.append(f"{code} 超过事件风险买入上限 {max_buys} 笔，跳过")
            continue
        try:
            pos = float(o.get("position"))
        except (TypeError, ValueError):
            pos = None
        if pos is not None and pos > cap:
            notes.append(f"{code} 目标仓位 {pos:g}% → {cap:g}%（{tag}）")
            o["position"] = round(cap, 2)
        if o.get("position") is None:
            o["position"] = round(cap, 2)
            notes.append(f"{code} 未给仓位，按事件上限 {cap:g}% 执行")
        if benefit:
            notes.append(f"{code} 属事件受益方向「{benefit}」，优先保留")
        buys += 1
        out.append(o)
    if notes:
        notes.insert(0, f"{tag}：单票≤{cap:g}%、最多 {max_buys} 笔买入")
    return out, notes
