#!/usr/bin/env python3
"""板块波动监控器：事件驱动激活盘中研究 + 决策。

替代固定时间表（10:00/11:00/13:30/14:45）：
- 交易日盘中每 interval_sec 秒拉一次东财全行业板块指数快照；
- 用量比/振幅/涨跌预筛，再对可疑板块复核 ATR + 布林带宽 + 量能；
- 满足 min_signals 个信号（默认 2/3）即"波动大"→ 激活：
    1) scripts.intraday_research.run(force=True)  刷新事件/板块资料
    2) flock 锁保护下跑一次 python3 -m sim.run     决策+执行
- 整个静默窗口超过 quiet_minutes（默认 60 分钟）→ 强制激活一次；
- 每日最多 max_activations 次，两次激活间隔至少 min_cooldown_min。

用法：
  .venv/bin/python scripts/volatility_watcher.py --loop      # 常驻
  .venv/bin/python scripts/volatility_watcher.py --once      # 单次检查
  .venv/bin/python scripts/volatility_watcher.py --force     # 立即激活
  .venv/bin/python scripts/volatility_watcher.py --status --verbose

并发保护：激活时用 flock 独占 logs/sim.lock；cron 的固定 sim.run
（如仍保留）也应加 flock，避免同时决策。
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from agent import volatility_watch as vw

LOCK_PATH = os.path.join(BASE_DIR, "logs", "sim.lock")
STATE_PATH = vw.STATE_PATH


def record_activation(state, res, ts=None):
    ts = ts or datetime.now().isoformat(timespec="seconds")
    if state.get("last_ts") == ts:
        return state
    state["last_ts"] = ts
    state["activated"] = int(state.get("activated") or 0) + 1
    log = state.setdefault("activations", [])
    log.append({
        "ts": ts,
        "force": bool(res.get("force")),
        "sector": (res.get("sector") or {}).get("name"),
        "code": (res.get("sector") or {}).get("code"),
        "ind": (res.get("sector") or {}).get("ind"),
        "reason": res.get("reason"),
    })
    if len(log) > 60:
        log[:] = log[-60:]
    return state


def _decision_locked():
    """sim.run 正在跑则返回 True（非阻塞探测）。"""
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
    except OSError:
        return True


def run_research(use_foreign=True):
    try:
        from scripts import intraday_research
        intraday_research.run(force=True, use_foreign=use_foreign)
    except SystemExit:
        pass
    except Exception as e:
        print(f"[watcher] 资料刷新失败: {type(e).__name__}: {e}",
              file=sys.stderr)


def activate(res, do_research=True, do_decision=True):
    if do_research:
        print(f"[watcher] >>> 激活 {datetime.now().isoformat(timespec='seconds')}"
              f" reason={res.get('reason')}", flush=True)
        run_research()
    if not do_decision:
        return 0
    if _decision_locked():
        print("[watcher] sim.run 正在决策，本轮跳过", flush=True)
        return 0
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    cmd = ["flock", "-n", LOCK_PATH, sys.executable, "-m", "sim.run",
           "--no-backup"]
    print(f"[watcher] 触发决策 {' '.join(cmd[3:])}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=BASE_DIR, timeout=1800)
        return r.returncode
    except subprocess.TimeoutExpired:
        print("[watcher] 决策超时(30min)，已终止", file=sys.stderr)
        return 1


def _not_trading_today():
    try:
        from trading.calendar import TradingCalendar
        return not TradingCalendar().is_trading_day(date.today())
    except Exception:
        return date.today().weekday() >= 5


def once(cfg=None, do_activate=True, force=False, verbose=False):
    """单周期检查。返回动作字符串 activate/wait/skip。"""
    state = vw.load_state(STATE_PATH)
    cache = {}
    if force:
        res = {"action": "activate", "reason": "手动/外部强制激活",
               "force": True}
    else:
        res = vw.check(state, cfg, verify_cache=cache)
    if verbose:
        snap = {k: state.get(k) for k in
                ("last_snap_at", "last_snap_size", "snap_fail", "day",
                 "activated", "last_ts")}
        print(f"[watcher] state={snap}", flush=True)
        print(f"[watcher] res={res}", flush=True)
    if res.get("action") == "activate" and do_activate:
        record_activation(state, res)
        activate(res)
    vw.save_state(state, STATE_PATH)
    return res.get("action")


def loop(cfg=None, do_activate=True):
    interval = float((cfg or {}).get("interval_sec", 60))
    print(f"[watcher] 启动（间隔{interval:.0f}s，静默"
          f"{(cfg or {}).get('quiet_minutes', 60)}min 强制激活）", flush=True)
    while True:
        if _not_trading_today() or not vw.in_session():
            time.sleep(60)
            continue
        try:
            once(cfg, do_activate=do_activate)
        except Exception as e:
            print(f"[watcher] 周期异常: {type(e).__name__}: {e}",
                  file=sys.stderr)
        time.sleep(max(10.0, interval))


def status():
    state = vw.load_state(STATE_PATH)
    start = state.get("session_started_at") or 0
    last = state.get("last_ts") or "-"
    quiet = ((datetime.now() - datetime.fromisoformat(last)).total_seconds()
             / 60 if last != "-" else "未激活")
    print(f"day={state.get('day')} activated={state.get('activated')}"
          f"/今日  last={last}  距上次激活={quiet}min")
    if start:
        print(f"session_started_at="
              f"{datetime.fromtimestamp(start).isoformat(timespec='seconds')}\n"
              f"last_snapshot={state.get('last_snap_at')} "
              f"({state.get('last_snap_size', 0)}个板块, "
              f"连续失败{state.get('snap_fail', 0)}次)")
    for a in (state.get("activations") or [])[-8:]:
        print(f"  {a['ts']} force={a.get('force')} "
              f"{a.get('sector') or '-'} {a.get('reason')}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="板块波动监控器")
    p.add_argument("--loop", action="store_true", help="常驻循环")
    p.add_argument("--once", action="store_true", help="单次检查")
    p.add_argument("--force", action="store_true", help="立即激活一次")
    p.add_argument("--status", action="store_true", help="看状态")
    p.add_argument("--interval", type=float, default=0)
    p.add_argument("--quiet", type=float, default=0,
                   help="静默强制激活阈值（分钟）")
    p.add_argument("--no-decision", action="store_true",
                   help="只刷新资料，不跑决策")
    args = p.parse_args(argv)

    cfg = {}
    try:
        import yaml
        with open(os.path.join(BASE_DIR, "config", "stock.yaml"),
                  encoding="utf-8") as f:
            cfg = ((yaml.safe_load(f) or {}).get("stock") or {}).get(
                "volatility_watch") or {}
    except Exception:
        cfg = {}
    if args.interval > 0:
        cfg["interval_sec"] = args.interval
    if args.quiet > 0:
        cfg["quiet_minutes"] = args.quiet

    if args.status:
        return status()
    if args.force:
        once(cfg, do_activate=True, force=True)
        return 0
    if args.loop:
        loop(cfg)
        return 0
    once(cfg, do_activate=not args.no_decision, verbose=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
