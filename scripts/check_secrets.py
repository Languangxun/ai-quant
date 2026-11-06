#!/usr/bin/env python3
"""提交前密钥/代理配置检查：防止把 KEY、Clash 订阅、节点配置推上 GitHub。

用法：
  python3 scripts/check_secrets.py            # 扫描 git 已跟踪文件
  python3 scripts/check_secrets.py --staged   # 只扫描暂存区（pre-commit）
  python3 scripts/check_secrets.py path...    # 扫描指定文件

只输出 文件:行号 + 规则名，不打印命中的密钥内容。
"""
import argparse
import os
import re
import subprocess
import sys

SKIP_DIRS = {
    ".git", ".venv", "__pycache__", "archive", "logs", "sim/state",
    "memory/daily", "memory/inbox", "backtest/results", "node_modules",
    ".pytest_cache", ".deploy_backup",
}
SKIP_SUFFIX = (".db", ".db-wal", ".db-shm", ".png", ".jpg", ".jpeg",
               ".gif", ".zip", ".gz", ".pdf", ".pyc", ".so", ".bin")
MAX_BYTES = 2 * 1024 * 1024

RULES = (
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("env-secret", re.compile(
        r"(?im)^\s*(?:DEEPSEEK_API_KEY|OPENAI_API_KEY|MAIL_AUTH_CODE|"
        r"SMTP_PASS|IMAP_PASS|CLASH_SUBSCRIPTION_URL)\s*=\s*"
        r"(?!<|your|xxx|test|example)[^\s#]{6,}")),
    ("clash-config", re.compile(
        r"(?im)^\s*(?:mixed-port|proxy-groups|external-controller)\s*:")),
    ("subscription-url", re.compile(
        r"https?://[^\s\"']*(?:clash=3|/subscribe|"
        r"subscription)[^\s\"']*", re.I)),
)


def _tracked():
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], text=True, stderr=subprocess.DEVNULL)
        return [x for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def _staged():
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            text=True, stderr=subprocess.DEVNULL)
        return [x for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def _skip(path):
    norm = path.replace(os.sep, "/")
    if any(norm == d or norm.startswith(d + "/") for d in SKIP_DIRS):
        return True
    if norm.endswith(SKIP_SUFFIX):
        return True
    # 示例配置允许保留占位符/说明
    if norm.endswith(".example.env") or norm.endswith(".example.yaml"):
        return True
    return False


def scan(paths):
    hits = []
    for path in paths:
        if _skip(path) or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, "rb") as f:
                raw = f.read()
            if b"\x00" in raw:
                continue
            text = raw.decode("utf-8", "replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in RULES:
                if pattern.search(line):
                    hits.append((path, lineno, name))
                    break
    return hits


def main(argv=None):
    p = argparse.ArgumentParser(description="检查密钥/Clash 配置是否误入库")
    p.add_argument("--staged", action="store_true", help="只查暂存区")
    p.add_argument("files", nargs="*", help="指定文件")
    args = p.parse_args(argv)
    paths = args.files or (_staged() if args.staged else _tracked())
    hits = scan(paths)
    if hits:
        print("发现疑似密钥/代理配置，已阻止提交：", file=sys.stderr)
        for path, lineno, name in hits:
            print(f"  {path}:{lineno}  [{name}]", file=sys.stderr)
        print("请移到 .env / ~/.config/ai-quant/ 并确认 .gitignore。",
              file=sys.stderr)
        return 1
    print(f"检查通过：{len(paths)} 个文件，无密钥/Clash 配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
