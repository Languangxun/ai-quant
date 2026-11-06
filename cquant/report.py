from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _pct(x: float, digits: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x * 100:+.{digits}f}%"


def _num(x: float, digits: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "—"
    return f"{x:.{digits}f}"


def _table(df: pd.DataFrame, cols: list[tuple[str, str]], empty: str = "—") -> str:
    if df is None or df.empty:
        return f"_{empty}_\n"
    head = "| " + " | ".join(h for h, _ in cols) + " |"
    sep = "|" + "|".join(["---:" if fmt != "s" else "---" for _, fmt in cols]) + "|"
    lines = [head, sep]
    for _, row in df.iterrows():
        cells = []
        for key, fmt in cols:
            v = row.get(key, np.nan)
            if fmt == "s":
                cells.append(str(v))
            elif fmt == "pct":
                cells.append(_pct(v))
            elif fmt == "pct0":
                cells.append(_pct(v, 0))
            elif fmt == "num":
                cells.append(_num(v))
            elif fmt == "num0":
                cells.append(_num(v, 0))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_report(path: Path, res: dict, cfg: dict) -> None:
    meta = res["meta"]
    summary = res["summary"]
    bench = res["benchmark"]
    yearly = res["yearly"]
    lev = res["leverage"]
    diag = res["diagnostics"]
    latest = res["latest"]

    prim = summary[summary["variant"] == res["primary"]].iloc[0]
    btc = bench.get("btc", {})
    ew = bench.get("equal_weight", {})

    lines = []
    lines.append("# 加密市场双模型融合回测（v0.1）\n")
    lines.append(
        f"- 生成时间：{meta['generated_at']}；模型来源：`{meta['source']}`\n"
        f"- 数据：{meta['data_start']} ~ {meta['data_end']}，"
        f"{meta['n_codes']} 个 USDT 永续合约（日线，24/7）\n"
        f"- 样本外区间：{meta['sim_start']} ~ {meta['sim_end']}"
        f"（{meta['n_days']} 日，调仓 {meta['rebalances']} 次，周期 {meta['horizon']} 日）\n"
        f"- 初始资金 {meta['capital']:,.0f} USDT，目标杠杆 {meta['leverage']:g}x，"
        f"单边手续费 {cfg['portfolio']['fee_rate']:.3%} + 滑点 "
        f"{cfg['portfolio']['slippage']:.3%}，资金费 "
        f"{cfg['portfolio']['funding_daily']:.3%}/日\n")

    mode = cfg["portfolio"].get("hedge_mode", "none")
    mode_txt = {
        "none": "纯多头杠杆（默认）：只买多头候选，空仓或持有现金",
        "btc": "多头顶部 alpha + 按账本 beta 做空 BTC，追求 beta 中性残差",
        "cross_section": "截面多空：多 top、空 bottom，不使用 BTC 对冲",
    }.get(mode, str(mode))
    lines.append("\n## 1. 双模型融合逻辑\n")
    lines.append(
        "1. **进攻端（sm701 路线）**：40 维技术因子 + 对 BTC 的 90 日滚动 beta，"
        "标签是未来 h 日 beta 中性残差收益 `R_ex = R - beta·R_btc` 是否跑赢当日截面中位数，"
        "LightGBM 输出 P(跑赢 BTC)。\n"
        "2. **防守端（sm702 路线）**：120 维个币价量/形态因子，标签是「未来 10 日内先跌 8% "
        "而非先涨 3%」的顶部概率，LightGBM 输出 P(顶部)。\n"
        "3. **融合**：当日截面把两个概率转成 [-1,1] 的百分位分数，"
        "`fused = z(alpha) − λ·z(risk)`；多头再叠加硬门槛 `risk < gate` 与"
        "当日风险分位约束；组合层用截面 risk 中位数做自适应 regime 降杠杆。\n"
        f"4. **组合**：{mode_txt}。按目标波动缩放 gross，"
        "逐日计资金费，盘中按 low/high 检查维持保证金并强平。\n")

    lines.append("\n## 2. 组合表现\n")
    rows = summary.copy()
    lines.append(_table(rows, [
        ("variant", "s"), ("total_return", "pct"), ("ann_return", "pct"),
        ("ann_vol", "pct"), ("sharpe", "num"), ("mdd", "pct"),
        ("calmar", "num"), ("avg_gross", "num"), ("avg_net", "num"),
        ("liquidations", "num0"), ("fee_paid", "num0"), ("funding_paid", "num0")]))
    lines.append(
        f"\n- 主策略 `{res['primary']}` 年化 **{_pct(prim['ann_return'])}**，"
        f"Sharpe **{_num(prim['sharpe'])}**，最大回撤 **{_pct(prim['mdd'])}**，"
        f"平均 gross {_num(prim['avg_gross'])}x。\n"
        f"- BTC 买入持有：年化 {_pct(btc.get('ann_return'))}，"
        f"最大回撤 {_pct(btc.get('mdd'))}；等权币池：年化 "
        f"{_pct(ew.get('ann_return'))}，最大回撤 {_pct(ew.get('mdd'))}。\n")

    lines.append("\n## 3. 逐年（主策略）\n")
    lines.append(_table(yearly, [("year", "num0"), ("return", "pct"),
                                 ("sharpe", "num"), ("mdd", "pct"),
                                 ("days", "num0")]))

    lines.append("\n## 4. 消融：两个模型各自贡献\n")
    lines.append(_table(summary, [("variant", "s"), ("ann_return", "pct"),
                                  ("sharpe", "num"), ("mdd", "pct"),
                                  ("calmar", "num"), ("avg_gross", "num")]))
    lines.append("\n`alpha_only_long` = 只用 sm701 排序、纯多头；"
                 "`risk_overlay_long` = 不惩罚排序，只用顶部概率做硬门槛与 regime；"
                 "`fused` = 排序惩罚 + 门槛 + regime；"
                 "`fused_xs` = 同样的融合分但走截面多空（对照组）。\n")

    lines.append("\n## 5. 杠杆敏感度（关闭波动目标，只看杠杆本身）\n")
    lines.append(_table(lev, [("variant", "s"), ("target_leverage", "num"),
                              ("ann_return", "pct"), ("ann_vol", "pct"),
                              ("sharpe", "num"), ("mdd", "pct"),
                              ("calmar", "num"), ("liquidations", "num0")]))
    lines.append("\n默认配置保留波动率目标，实际 gross 会被压到目标水平以下；"
                 "上表是为了单独衡量杠杆，把 vol_target 设为 0 后的固定杠杆结果。"
                 "杠杆越高，2025-10-10 全市场闪崩造成的强平风险越大。\n")

    lines.append("\n## 6. 模型诊断（样本外）\n")
    lines.append(_table(diag, [("year", "s"), ("source", "s"),
                               ("n_test", "num0"), ("best_iter_alpha", "num0"),
                               ("best_iter_risk", "num0"), ("alpha_ic", "num"),
                               ("risk_auc", "num")]))
    lines.append("\n`alpha_ic` 为逐日 RankIC 均值（对 beta 中性残差收益）；"
                 "`risk_auc` 为顶部标签的样本外 AUC。\n")

    lines.append("\n## 7. 最新目标仓位（信号日）\n")
    if latest is not None and not latest.empty:
        show = latest.copy()
        show["side"] = np.where(show["weight"] > 0, "多", "空")
        lines.append(_table(show.head(30), [
            ("date", "s"), ("code", "s"), ("side", "s"), ("close", "num"),
            ("alpha", "num"), ("risk", "num"), ("weight", "pct"),
            ("notional", "num0")]))
        gross = float(show["weight"].abs().sum())
        lines.append(f"\n合计 gross {gross:.2f}x（按初始资金 {meta['capital']:,.0f} "
                     f"USDT 折算名义敞口）。\n")
    else:
        lines.append("_无目标仓位_\n")

    lines.append("\n## 8. 口径与限制\n")
    lines.append(
        "- 数据为 sm701 `stock_cache.db` 的 `crypto_daily`（USDT 永续日线），"
        "`vol` 视为 USDT 成交额；当前币池是「活到 2026-11 的 100 个合约」，"
        "存在上市时间/生存偏差，2022 年可用标的仅约 48 个。\n"
        "- 日线撮合：t 收盘出信号、t+1 开盘成交；未建模盘口深度、冲击成本、"
        "交易所限频与借币可得性。强平按日内 low/high 近似，"
        "维持保证金按 gross 的 1% 线性处理。\n"
        "- 资金费按 gross 收取固定 0.01%/日（保守），未区分多空方向；"
        "实际永续资金费随基差波动。\n"
        "- 杠杆来自组合名义敞口，不做逐仓；`vol_target` 与 regime 降杠杆都在信号层，"
        "不会使用未来数据。\n"
        "- 这是研究/回测项目，不含实盘下单、API Key、LLM 决策。\n")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
