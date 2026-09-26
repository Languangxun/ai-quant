#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_predict_to_pi.py - 桌面 stock_predict.py 变更自动同步到 Orange Pi

源：/home/lan/桌面/stock_predict/stock_predict.py
    （stock_gui.py 的唯一生成物 CLI，build_cli.py 每次生成后自动同步）
目标：orangepi@192.168.3.28:~/ai-quant/scripts/cli/stock_predict.py

行为：轮询源文件 sha256，发现变化并稳定后：
  1. 本地编译校验（Python 3.14）
  2. rsync 到远端临时名
  3. 远端编译校验（Pi 是 Python 3.12，拦下新语法不兼容）
  4. 备份原文件为 .bak，原子 mv 覆盖
  5. 比对远端 sha256，不一致视为失败

用法：
  python3 scripts/sync_predict_to_pi.py            # 前台监听（默认每 3s 轮询）
  python3 scripts/sync_predict_to_pi.py --daemon   # 后台常驻
  python3 scripts/sync_predict_to_pi.py --once     # 同步一次就退出
  python3 scripts/sync_predict_to_pi.py --status   # 查看后台状态
  python3 scripts/sync_predict_to_pi.py --stop     # 停止后台进程

日志：~/.cache/sync_predict_to_pi/sync.log（--log 改）
状态：~/.cache/sync_predict_to_pi/state.json
"""
import argparse
import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

APP = "sync_predict_to_pi"
DEFAULT_SRC = "/home/lan/桌面/stock_predict/stock_predict.py"
DEFAULT_HOST = "orangepi@192.168.3.28"
DEFAULT_DEST = "~/ai-quant/scripts/cli"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", APP)
DEFAULT_LOG = os.path.join(CACHE_DIR, "sync.log")
PID_FILE = os.path.join(CACHE_DIR, "pid")
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]
COMPILE_CODE = ('import sys; compile(open(sys.argv[1], encoding="utf-8")'
                '.read(), sys.argv[1], "exec")')

log = logging.getLogger(APP)


def setup_logging(path, foreground=True):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if foreground:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def run(cmd, timeout=60, check=False):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=check)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_compile_ok(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    compile(src, path, "exec")


class Remote:
    def __init__(self, host, timeout=60):
        self.host = host
        self.timeout = timeout
        self.home = None

    def ssh(self, remote_cmd, timeout=None, check=False):
        cmd = ["ssh", *SSH_OPTS, self.host, remote_cmd]
        return run(cmd, timeout=timeout or self.timeout, check=check)

    def resolve_home(self):
        if self.home:
            return self.home
        r = self.ssh('printf %s "$HOME"')
        home = (r.stdout or "").strip()
        if r.returncode != 0 or not home:
            raise RuntimeError("无法获取远端 $HOME：%s"
                               % (r.stderr or r.stdout or "").strip())
        self.home = home
        return home

    def path(self, dest):
        if dest == "~":
            return self.resolve_home()
        if dest.startswith("~/"):
            return self.resolve_home() + dest[1:]
        return dest

    def exists(self, path):
        return self.ssh("test -e %s" % shlex.quote(path)).returncode == 0

    def sha256(self, path):
        r = self.ssh("sha256sum -- %s" % shlex.quote(path))
        if r.returncode != 0:
            return None
        out = (r.stdout or "").strip().split()
        return out[0] if out else None

    def compile_ok(self, path):
        cmd = "python3 -c %s %s" % (shlex.quote(COMPILE_CODE),
                                    shlex.quote(path))
        r = self.ssh(cmd)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def rsync_to(remote, local, remote_tmp, timeout=120):
    rsh = "ssh " + " ".join(SSH_OPTS)
    cmd = ["rsync", "-a", "-e", rsh, "--", local,
           "%s:%s" % (remote.host, remote_tmp)]
    return run(cmd, timeout=timeout)


def deploy(args, remote, src, local_hash=None):
    """推一次：返回 True 表示远端已与本地一致。"""
    name = os.path.basename(src)
    dest_dir = remote.path(args.dest)
    dest = dest_dir + "/" + name
    tmp = "%s/.%s.tmp.%d" % (dest_dir, name, os.getpid())
    t0 = time.time()
    if local_hash is None:
        local_hash = sha256_file(src)
    size = os.path.getsize(src)

    local_compile_ok(src)
    log.info("开始推送 %s（%.1f KB，sha=%s）", name, size / 1024.0,
             local_hash[:12])
    if args.dry_run:
        log.info("--dry-run：跳过实际推送，目标 %s:%s", remote.host, dest)
        return True

    r = remote.ssh("mkdir -p -- %s" % shlex.quote(dest_dir))
    if r.returncode != 0:
        raise RuntimeError("远端建目录失败：%s" % (r.stderr or "").strip())

    r = rsync_to(remote, src, tmp)
    if r.returncode != 0:
        remote.ssh("rm -f -- %s" % shlex.quote(tmp))
        raise RuntimeError("rsync 失败：%s" % (r.stderr or r.stdout).strip())

    ok, err = remote.compile_ok(tmp)
    if not ok:
        remote.ssh("rm -f -- %s" % shlex.quote(tmp))
        raise RuntimeError("远端语法校验失败（Python 3.12 不兼容？）：%s" % err)

    if not args.no_backup and remote.exists(dest):
        remote.ssh("cp -p -- %s %s"
                   % (shlex.quote(dest), shlex.quote(dest + ".bak")))

    r = remote.ssh("mv -f -- %s %s" % (shlex.quote(tmp), shlex.quote(dest)))
    if r.returncode != 0:
        remote.ssh("rm -f -- %s" % shlex.quote(tmp))
        raise RuntimeError("远端覆盖失败：%s" % (r.stderr or "").strip())

    remote_hash = remote.sha256(dest)
    if remote_hash != local_hash:
        raise RuntimeError("远端校验不一致：local=%s remote=%s"
                           % (local_hash[:12], (remote_hash or "?")[:12]))
    log.info("推送完成 %s:%s（%.2fs，sha=%s）", remote.host, dest,
             time.time() - t0, remote_hash[:12])
    return True


def wait_settle(src, settle, max_wait=60.0):
    """等文件内容稳定：连续 settle 秒 sha256 不变；返回稳定后的 hash。"""
    t0 = time.time()
    last = None
    stable_since = None
    while time.time() - t0 < max_wait:
        try:
            h = sha256_file(src)
        except OSError:
            time.sleep(0.3)
            continue
        now = time.time()
        if h != last:
            last, stable_since = h, now
        elif now - stable_since >= settle:
            return h
        time.sleep(0.3)
    return last


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(local_hash, remote, dest, src=None):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    data = {
        "synced_hash": local_hash,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": src,
        "host": remote.host,
        "dest": dest,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)
    return data


def sync_once(args, remote, src, force=False):
    """同步一次。返回 0=已最新/推送成功，1=失败。"""
    if not os.path.exists(src):
        log.error("源文件不存在：%s", src)
        return 1
    local_hash = sha256_file(src)
    dest = remote.path(args.dest).rstrip("/") + "/" + os.path.basename(src)
    if not force:
        try:
            remote_hash = remote.sha256(dest)
        except Exception as e:
            log.error("检查远端失败：%s", e)
            return 1
        if remote_hash == local_hash:
            log.info("远端已是最新（sha=%s），无需推送", local_hash[:12])
            save_state(local_hash, remote, args.dest, args.src)
            return 0
    try:
        deploy(args, remote, src, local_hash)
        save_state(local_hash, remote, args.dest, args.src)
        return 0
    except Exception as e:
        log.error("推送失败：%s", e)
        return 1


def watch(args, remote, src):
    stop = {"v": False}

    def _handler(signum, frame):
        stop["v"] = True
        log.info("收到信号 %s，退出监听", signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    last = None
    if not args.force:
        try:
            local_hash = sha256_file(src)
            dest = remote.path(args.dest).rstrip("/") + "/" + os.path.basename(src)
            remote_hash = remote.sha256(dest)
            if remote_hash == local_hash:
                last = local_hash
                save_state(local_hash, remote, args.dest, args.src)
                log.info("启动检查：远端已是最新（sha=%s）", local_hash[:12])
            else:
                log.info("启动检查：远端 %s ≠ 本地 %s，开始同步",
                         (remote_hash or "缺失")[:12], local_hash[:12])
        except Exception as e:
            log.warning("启动检查失败（稍后轮询重试）：%s", e)

    fails = 0
    while not stop["v"]:
        try:
            if not os.path.exists(src):
                time.sleep(args.interval)
                continue
            h = sha256_file(src)
            if h != last:
                h = wait_settle(src, args.settle)
                if h != last:
                    log.info("检测到变化（sha=%s）", h[:12])
                    deploy(args, remote, src, h)
                    last = h
                    save_state(h, remote, args.dest, args.src)
                fails = 0
        except Exception as e:
            fails += 1
            delay = min(60.0, args.interval * (2 ** min(fails, 5)))
            log.error("同步失败（第 %d 次，%.0fs 后重试）：%s", fails, delay, e)
            end = time.time() + delay
            while not stop["v"] and time.time() < end:
                time.sleep(0.5)
            continue
        end = time.time() + args.interval
        while not stop["v"] and time.time() < end:
            time.sleep(0.5)
    log.info("监听结束")


def read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_stop():
    pid = read_pid()
    if not pid_alive(pid):
        print("后台未运行")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    if pid_alive(pid):
        print("进程 %d 未退出，请手动处理" % pid)
        return 1
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    print("已停止（pid=%d）" % pid)
    return 0


def cmd_status():
    pid = read_pid()
    print("进程：%s" % ("运行中 pid=%d" % pid if pid_alive(pid)
                      else ("已退出（残留 pid=%s）" % pid if pid else "未运行")))
    st = load_state()
    if st:
        print("上次同步：%s  sha=%s" % (st.get("time"),
                                   (st.get("synced_hash") or "?")[:12]))
        print("目标：%s:%s" % (st.get("host"), st.get("dest")))
    else:
        print("暂无同步记录")
    return 0


def daemonize(args):
    if pid_alive(read_pid()):
        print("后台已在运行（pid=%d），先 --stop" % read_pid())
        return 1
    pid = os.fork()
    if pid > 0:
        print("已后台启动 pid=%d，日志 %s" % (pid, args.log))
        return 0
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    setup_logging(args.log, foreground=False)
    log.info("后台启动 pid=%d src=%s -> %s:%s", os.getpid(), args.src,
             args.host, args.dest)
    remote = Remote(args.host, args.timeout)
    watch(args, remote, args.src)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description="桌面 stock_predict.py 变更自动同步到 Orange Pi")
    p.add_argument("--src", default=DEFAULT_SRC, help="源文件")
    p.add_argument("--host", default=DEFAULT_HOST, help="目标主机 user@host")
    p.add_argument("--dest", default=DEFAULT_DEST,
                   help="目标目录（支持 ~/，默认 ~/ai-quant/scripts/cli）")
    p.add_argument("--interval", type=float, default=3.0,
                   help="轮询间隔秒（默认 3）")
    p.add_argument("--settle", type=float, default=1.5,
                   help="内容稳定判定秒（默认 1.5）")
    p.add_argument("--timeout", type=float, default=60.0, help="ssh 超时秒")
    p.add_argument("--log", default=DEFAULT_LOG, help="日志文件")
    p.add_argument("--once", action="store_true", help="同步一次后退出")
    p.add_argument("--force", action="store_true", help="忽略已同步判断，强制推送")
    p.add_argument("--daemon", action="store_true", help="后台常驻监听")
    p.add_argument("--stop", action="store_true", help="停止后台进程")
    p.add_argument("--status", action="store_true", help="查看状态")
    p.add_argument("--dry-run", action="store_true", help="只检查不推送")
    p.add_argument("--no-backup", action="store_true", help="远端不留 .bak")
    return p


def main():
    args = build_parser().parse_args()
    if args.stop:
        return cmd_stop()
    if args.status:
        return cmd_status()
    if args.daemon:
        return daemonize(args)
    setup_logging(args.log, foreground=True)
    remote = Remote(args.host, args.timeout)
    if args.once:
        return sync_once(args, remote, args.src, force=args.force)
    log.info("开始监听 %s -> %s:%s（间隔 %.1fs）", args.src, args.host,
             args.dest, args.interval)
    watch(args, remote, args.src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
