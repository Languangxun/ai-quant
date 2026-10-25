#!/usr/bin/env python3
"""pi 邮件汇报。用法:
  python3 send_report.py               # 发今日复盘 + sim.log 尾部 (去重: 当日已发则跳过)
  python3 send_report.py daily-force   # 强制重发今日复盘
  python3 send_report.py replay-final  # 发训练回放最终报告
  python3 send_report.py setup-fail "<msg>"   # 部署失败告警
  python3 send_report.py setup-ok "<msg>"     # 部署完成通知

可靠性: 网口链路抖动 (Link is Down) 会让 DNS 解析瞬时失败 ->
socket.gaierror。这里做 5 次退避重试, 仍失败则用公共 DNS 直查 A 记录
把 smtp 主机钉到 IP (SNI/证书仍用域名) 后再发。
"""
import os
import random
import socket
import smtplib
import ssl
import struct
import subprocess
import sys
import time
from datetime import date
from email.header import Header
from email.mime.text import MIMEText

MAIL_ENV = os.path.expanduser("~/.config/ai-quant/mail.env")
FALLBACK_DNS = ("223.5.5.5", "119.29.29.29", "1.1.1.1")
RETRY_DELAYS = (0, 3, 8, 20, 45)
SENT_MARK_DIR = os.path.expanduser("~/.cache/ai_quant")


def load_mail_config():
    """从 ~/.config/ai-quant/mail.env (KEY=VALUE) 读凭据, 缺项回退环境变量。"""
    conf = {}
    try:
        with open(MAIL_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for key in ("MAIL_HOST", "MAIL_PORT", "MAIL_USER", "MAIL_AUTH_CODE",
                "MAIL_RECIPIENTS"):
        if conf.get(key):
            continue
        env_key = key
        val = os.environ.get(env_key)
        if val:
            conf[key] = val
    return conf


CONF = load_mail_config()
SMTP_HOST = CONF.get("MAIL_HOST") or "smtp.163.com"
SMTP_PORT = int(CONF.get("MAIL_PORT") or 465)
SENDER = CONF.get("MAIL_USER") or ""
AUTH_CODE = CONF.get("MAIL_AUTH_CODE") or ""
RECIPIENTS = [x.strip() for x in
               (CONF.get("MAIL_RECIPIENTS") or "").split(",") if x.strip()]


def _dns_a(host, server):
    """用公共 DNS 直接查 A 记录 (标准库 UDP, 不依赖系统 resolver)。"""
    tid = random.randint(0, 0xFFFF)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split("."))
    qname += b"\x00"
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    packet = header + qname + struct.pack("!HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    try:
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    if len(data) < 12 or struct.unpack("!H", data[:2])[0] != tid:
        return []
    qd, an = struct.unpack("!HH", data[4:8])
    off = 12
    for _ in range(qd):
        while data[off] != 0:
            off += data[off] + 1
        off += 5
    ips = []
    for _ in range(an):
        if data[off] & 0xC0 == 0xC0:
            off += 2
        else:
            while data[off] != 0:
                off += data[off] + 1
            off += 1
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in rdata))
    return ips


def resolve_via_public_dns(host):
    for server in FALLBACK_DNS:
        try:
            ips = _dns_a(host, server)
        except Exception:
            continue
        if ips:
            return ips
    return []


class _PinnedAddr:
    """临时把 host 解析钉到指定 IP (SNI/证书仍用原域名)。"""

    def __init__(self, ips, port):
        self._ips = list(ips)
        self._port = port
        self._orig = None

    def __enter__(self):
        self._orig = socket.getaddrinfo
        ips = list(self._ips)

        def fake(host, port, *args, **kwargs):
            if not ips:
                return self._orig(host, port, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (ip, self._port)) for ip in ips]

        socket.getaddrinfo = fake
        return self

    def __exit__(self, *exc):
        socket.getaddrinfo = self._orig
        return False


def _mark_path(key):
    return os.path.join(SENT_MARK_DIR, f"sent_{key}")


def mark_sent(key):
    try:
        os.makedirs(SENT_MARK_DIR, exist_ok=True)
        with open(_mark_path(key), "w", encoding="utf-8") as f:
            f.write(date.today().isoformat())
    except OSError:
        pass


def already_sent(key):
    return os.path.exists(_mark_path(key))


def _do_send(subject, body):
    if not SENDER or not AUTH_CODE or not RECIPIENTS:
        raise RuntimeError("邮件凭据未配置 (缺 MAIL_USER/MAIL_AUTH_CODE/"
                           "MAIL_RECIPIENTS, 详见 ~/.config/ai-quant/mail.env)")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = SENDER
    msg["To"] = ", ".join(RECIPIENTS)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as s:
        s.login(SENDER, AUTH_CODE)
        s.sendmail(SENDER, RECIPIENTS, msg.as_string())


def send(subject, body, dedupe_key=None, force=False):
    """带退避重试 + 公共 DNS 兜底的发送。返回 True/False。"""
    if dedupe_key and not force and already_sent(dedupe_key):
        print(f"skip (already sent today): {subject}")
        return True
    last = None
    for i, delay in enumerate(RETRY_DELAYS, 1):
        if delay:
            time.sleep(delay)
        try:
            _do_send(subject, body)
            print("mail sent:", subject)
            if dedupe_key:
                mark_sent(dedupe_key)
            return True
        except Exception as e:
            last = e
            print(f"[retry {i}/{len(RETRY_DELAYS)}] 发送失败: {e}", flush=True)
            if isinstance(e, socket.gaierror) or "Name or service" in str(e):
                ips = resolve_via_public_dns(SMTP_HOST)
                if ips:
                    print(f"[retry {i}] DNS 兜底: {SMTP_HOST} -> {ips}", flush=True)
                    try:
                        with _PinnedAddr(ips, SMTP_PORT):
                            _do_send(subject, body)
                        print("mail sent:", subject)
                        if dedupe_key:
                            mark_sent(dedupe_key)
                        return True
                    except Exception as e2:
                        last = e2
                        print(f"[retry {i}] 兜底发送仍失败: {e2}", flush=True)
    print(f"[fail] 邮件发送失败: {last}", flush=True)
    return False


def _review_paths(d):
    return [os.path.expanduser(f"~/ai-quant/memory/daily/股票复盘-{d}.md"),
            os.path.expanduser(f"~/ai-quant/memory/daily/复盘-{d}.md")]


def _sim_running():
    try:
        out = subprocess.run(["pgrep", "-f", "sim.run"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except Exception:
        return False


def wait_for_review(d, timeout=2700, interval=20):
    """等 14:45 时点的模拟盘跑完并落盘复盘 (网口抖动时该时点可能拖到 15:30+)."""
    deadline = time.time() + timeout
    paths = _review_paths(d)
    warned = False
    while time.time() < deadline:
        if not os.path.exists(paths[0]) and not os.path.exists(paths[1]):
            if _sim_running():
                if not warned:
                    print(f"[wait] 等待模拟盘结束以便纳入 14:45 时点...", flush=True)
                    warned = True
                time.sleep(interval)
                continue
            return None
        if _sim_running():
            if not warned:
                print("[wait] 等待模拟盘结束以便纳入 14:45 时点...", flush=True)
                warned = True
            time.sleep(interval)
            continue
        return next((x for x in paths if os.path.exists(x)), None)
    print("[wait] 等待超时, 用现有内容发送", flush=True)
    return next((x for x in paths if os.path.exists(x)), None)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "replay-final":
        log = os.path.expanduser("~/replay.log")
        body = "ai-quant 训练回放已跑完，最终报告如下：\n\n"
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            body += content[-4000:]
        else:
            body += "(replay.log 不存在)"
        ok = send("ai-quant 训练回放完成", body)
        return 0 if ok else 1
    if mode.startswith("setup-"):
        msg = sys.argv[2] if len(sys.argv) > 2 else ""
        ok = send("ai-quant pi 部署状态", f"{mode}: {msg}")
        return 0 if ok else 1
    if mode == "demo-vote":
        log = "/tmp/demo-vote.log"
        body = "ai-quant 双模型投票演示（未执行交易）：\n\n"
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="ignore") as f:
                body += f.read()
        else:
            body += "(demo-vote.log 不存在)"
        ok = send("ai-quant 双模型投票演示", body)
        return 0 if ok else 1
    d = date.today().isoformat()
    force = mode == "daily-force"
    path = wait_for_review(d) if not force else next(
        (x for x in _review_paths(d) if os.path.exists(x)), None)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            body = f.read()
    else:
        body = f"（今日复盘文件不存在：{d}，可能未运行或失败）"
    simlog = os.path.expanduser("~/ai-quant/logs/sim.log")
    if os.path.exists(simlog):
        with open(simlog, encoding="utf-8", errors="ignore") as f:
            tail = f.read()[-1500:]
        body += "\n\n--- sim.log 尾部 ---\n" + tail
    health = "/tmp/health.json"
    if os.path.exists(health):
        with open(health, encoding="utf-8") as f:
            body += "\n\n--- " + f.read()
    lessons = "/home/orangepi/ai-quant/memory/lessons.json"
    if os.path.exists(lessons):
        try:
            import json as _json
            with open(lessons, encoding="utf-8") as f:
                items = _json.load(f)
            if items:
                body += "\n\n--- 最近经验教训 ---\n" + "\n".join(
                    f"- [{x.get('date', '')}] {x.get('lesson', '')}"
                    for x in items[-3:]
                )
        except Exception:
            pass
    ok = send(f"ai-quant 模拟盘复盘 {d}", body, dedupe_key=f"daily_{d}",
              force=force)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
