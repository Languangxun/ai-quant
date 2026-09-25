#!/usr/bin/env python3
"""重置股票模拟账户：归档旧 state 并按本金重建（默认取 config/stock.yaml 的 capital）。

用法（在 ai-quant 目录下）：
  .venv/bin/python scripts/reset_account.py --capital 20000 --yes
  .venv/bin/python scripts/reset_account.py --show     # 只看当前本金/持仓
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.stock_account import StockAccount

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, "sim", "state", "stock_account.json")


def load_cfg():
    import yaml
    with open(os.path.join(BASE_DIR, "config", "stock.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f)["stock"]


def main(argv=None):
    cfg = load_cfg()
    ap = argparse.ArgumentParser(description="重置股票模拟账户")
    ap.add_argument("--capital", type=float, default=cfg["capital"],
                    help=f"新本金（默认 {cfg['capital']}，config/stock.yaml capital）")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--yes", action="store_true", help="确认清空旧账户")
    ap.add_argument("--show", action="store_true", help="仅打印当前账户概览")
    args = ap.parse_args(argv)

    if args.show:
        if not os.path.exists(args.state):
            print(f"无账户文件（首次运行将按本金 {args.capital:,.0f} 建盘）：{args.state}")
            return 0
        with open(args.state, encoding="utf-8") as f:
            st = json.load(f)
        lots = {c: sum(l["shares"] for l in v)
                for c, v in (st.get("lots") or {}).items() if v}
        print(f"本金 {st.get('initial_capital'):,.0f} 元 · 现金 {st.get('cash'):,.2f} · "
              f"已实现 {st.get('realized_pnl'):,.2f} · 成交 {st.get('trade_count')} 笔")
        print("持仓：", lots or "空仓")
        return 0

    if os.path.exists(args.state) and not args.yes:
        print(f"账户已存在：{args.state}")
        print("重置将清空持仓/收益并归档旧文件，确认请加 --yes（先看用 --show）")
        return 1

    if os.path.exists(args.state):
        archive = os.path.join(os.path.dirname(args.state), "archive")
        os.makedirs(archive, exist_ok=True)
        bak = os.path.join(
            archive, f"stock_account-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.move(args.state, bak)
        print(f"旧账户已归档：{bak}")

    account = StockAccount(
        args.capital,
        commission_rate=cfg["fees"]["commission_rate"],
        min_commission=cfg["fees"]["min_commission"],
        stamp_tax_rate=cfg["fees"]["stamp_tax_rate"],
        transfer_fee_rate=cfg["fees"]["transfer_fee_rate"],
    )
    tmp = args.state + ".tmp"
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(account.to_state(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.state)
    print(f"新账户本金 {args.capital:,.0f} 元 -> {args.state}")
    print("提示：历史 daily 汇总仍保留；如需完全重来，请一并清理 memory/daily/stock-*.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
