from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import data as data_mod
from . import fusion, models
from .audit import audit, prepare
from .backtest import log, run
from .config import load_config


def _parse_years(s: str) -> list[int]:
    return [int(x) for x in s.replace("，", ",").split(",") if x.strip()]


def cmd_backtest(cfg: dict, args) -> None:
    res = run(cfg, source=args.source,
              years=_parse_years(args.years) if args.years else None,
              start=args.start or None, end=args.end or None,
              leverage=args.leverage, ablations=args.ablations,
              out_dir=args.out or None, reuse_from=args.reuse or None)
    summary = res["summary"]
    print("\n" + summary.to_string(index=False))
    print(f"\n报告: {Path(cfg['output']['dir']) / 'report.md'}")


def cmd_predict(cfg: dict, args) -> None:
    ctx = prepare(cfg)
    df = ctx["df"]
    last = str(df["date"].max())
    pred = models.predict_date(cfg, ctx, last, source=args.source or "auto")
    src = pred.attrs.get("model_source", "unknown")
    base = df.loc[df["date"] == last, ["date", "code", "close", "hist_len",
                                       "turnover20"]].copy()
    base["code"] = base["code"].astype(str)
    base["beta"] = ctx["beta"].loc[df["date"] == last].to_numpy(dtype="float64")
    snap = pred.merge(base, on=["date", "code"], how="left")
    excl = set(cfg["data"].get("exclude_codes") or [])
    snap = snap[~snap["code"].isin(excl)].reset_index(drop=True)

    p = cfg["portfolio"]
    book = fusion.build_book(snap, cfg["fusion"], p, min(
        float(args.leverage or p["leverage"]), float(p["max_leverage"])))
    px = data_mod.price_pivots(df)
    ret_df = px["close"].pct_change()
    rs = fusion.regime_scale(book.meta.get("risk_median", np.nan),
                             cfg["fusion"]["regime"])
    vs = fusion.book_vol_scale(book.weights, ret_df, last,
                               float(p["vol_target"]), float(p["min_vol_scale"]),
                               max(1.0, float(p["max_leverage"]) / float(p["leverage"])))
    book.weights = {c: w * rs * vs for c, w in book.weights.items()}

    capital = float(args.capital or p["capital"])
    rows = []
    for code, w in sorted(book.weights.items(), key=lambda kv: -abs(kv[1])):
        r = snap[snap["code"] == code].iloc[0]
        rows.append({"date": last, "code": code,
                     "side": "LONG" if w > 0 else "SHORT",
                     "weight": w, "notional": w * capital,
                     "close": float(r["close"]),
                     "alpha": float(r["alpha"]), "risk": float(r["risk"])})
    out = pd.DataFrame(rows)
    out_dir = Path(cfg["output"]["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_dir / "latest_targets.csv", index=False)
    meta = {"date": last, "model_source": src, "capital": capital,
            "gross": float(out["weight"].abs().sum()),
            "regime_scale": rs, "vol_scale": vs,
            "risk_median": book.meta.get("risk_median"),
            "n_long": int((out["weight"] > 0).sum()),
            "n_short": int((out["weight"] < 0).sum()),
            "live_broker": False}
    with open(out_dir / "latest_targets.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f"\n{last} 目标仓位（模型 {src}，regime {rs:.2f}，波动缩放 {vs:.2f}）")
    print(out.to_string(index=False))
    print(f"\ngross {meta['gross']:.2f}x，名义敞口 {meta['gross'] * capital:,.0f} USDT")
    print("已写入 outputs/latest_targets.csv / .json（仅研究信号，不含实盘下单）")


def cmd_audit(cfg: dict, args) -> None:
    out = audit(cfg)
    out_dir = Path(cfg["output"]["dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_dir / "audit.csv", index=False)
    if not bool(out["passed"].all()):
        raise SystemExit("未来函数审计未通过")
    print("\n未来函数审计通过")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="cquant", description="加密双模型融合量化实验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="双模型融合回测")
    b.add_argument("--config", default="config/crypto.yaml")
    b.add_argument("--source", choices=["walkforward", "pretrained"], default=None)
    b.add_argument("--years", default="", help="逗号分隔，如 2022,2023,2024")
    b.add_argument("--start", default="")
    b.add_argument("--end", default="")
    b.add_argument("--leverage", type=float, default=None)
    b.add_argument("--no-ablations", dest="ablations", action="store_false",
                   default=None)
    b.add_argument("--out", default="", help="输出目录，默认取配置 output.dir")
    b.add_argument("--reuse", default="", help="复用已有 predictions 的目录")
    b.set_defaults(func=cmd_backtest)

    p = sub.add_parser("predict", help="输出最新目标仓位")
    p.add_argument("--config", default="config/crypto.yaml")
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--leverage", type=float, default=None)
    p.add_argument("--source", choices=["walkforward", "pretrained", "auto"],
                   default="auto")
    p.set_defaults(func=cmd_predict)

    a = sub.add_parser("audit", help="未来函数审计")
    a.add_argument("--config", default="config/crypto.yaml")
    a.set_defaults(func=cmd_audit)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "start", None):
        cfg["backtest"]["start"] = args.start
    args.func(cfg, args)


if __name__ == "__main__":
    main()
