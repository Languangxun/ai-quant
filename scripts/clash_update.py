#!/usr/bin/env python3
"""更新 Pi 上的 Clash/mihomo 订阅配置（不把订阅 URL 写进仓库）。

订阅 URL 读取顺序：
  1. ~/.config/ai-quant/clash/subscription.txt（推荐，chmod 600）
  2. 环境变量 CLASH_SUBSCRIPTION_URL
配置文件默认 ~/.config/mihomo/config.yaml；更新前自动备份，更新后
`mihomo -t` 校验并重启 user service。

用法：
  python3 scripts/clash_update.py            # 拉订阅 -> 校验 -> 重启
  python3 scripts/clash_update.py --check    # 只看当前配置/连通性
  python3 scripts/clash_update.py --no-restart
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

SUB_FILE = os.path.expanduser(
    "~/.config/ai-quant/clash/subscription.txt")
CONFIG = os.path.expanduser("~/.config/mihomo/config.yaml")
MIHOMO = os.path.expanduser("~/.local/bin/mihomo")
PROXY = (os.environ.get("CLASH_UPDATE_PROXY")
         or os.environ.get("FOREIGN_SEARCH_PROXY")
         or "http://127.0.0.1:7890")
UA = "clash-verge/v2.0"


def subscription_url():
    try:
        with open(SUB_FILE, encoding="utf-8") as f:
            url = f.read().strip()
        if url:
            return url
    except OSError:
        pass
    return os.environ.get("CLASH_SUBSCRIPTION_URL", "").strip()


def _fetch(url, proxy, timeout=40):
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy})] if proxy else [
        urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=timeout) as r:
        return r.read(8 * 1024 * 1024)


def _validate(raw):
    try:
        import yaml
        doc = yaml.safe_load(raw.decode("utf-8", "replace"))
    except Exception as e:
        return None, f"YAML 解析失败: {type(e).__name__}: {e}"
    if not isinstance(doc, dict):
        return None, "订阅内容不是 Clash YAML 对象"
    missing = [k for k in ("proxies", "proxy-groups", "rules")
               if k not in doc]
    if missing:
        return None, f"缺少字段: {', '.join(missing)}（可能是 base64 订阅）"
    return doc, ""


def _test(config):
    if not os.path.exists(MIHOMO):
        return True, "未找到 mihomo，跳过 -t 校验"
    try:
        r = subprocess.run(
            [MIHOMO, "-d", os.path.dirname(config), "-t"],
            capture_output=True, text=True, timeout=90)
        return r.returncode == 0, (r.stderr or r.stdout)[-400:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def update(no_restart=False, proxy=None):
    url = subscription_url()
    if not url:
        print("[clash] 未配置订阅：请写入 "
              "~/.config/ai-quant/clash/subscription.txt", file=sys.stderr)
        return 1
    proxy = proxy if proxy is not None else PROXY
    try:
        raw = _fetch(url, proxy)
    except Exception as e:
        print(f"[clash] 下载失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    doc, err = _validate(raw)
    if doc is None:
        print(f"[clash] {err}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    if os.path.exists(CONFIG):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(CONFIG, CONFIG + f".bak-{stamp}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG), suffix=".yaml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.chmod(tmp, 0o600)
        ok, msg = _test(tmp)
        if not ok:
            print(f"[clash] 新配置校验失败，保留旧配置：{msg}",
                  file=sys.stderr)
            return 1
        os.replace(tmp, CONFIG)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    print(f"[clash] 已更新 {CONFIG}（proxies={len(doc.get('proxies') or [])}，"
          f"groups={len(doc.get('proxy-groups') or [])}，"
          f"rules={len(doc.get('rules') or [])}）")
    if not no_restart:
        try:
            subprocess.run(["systemctl", "--user", "restart", "mihomo"],
                           check=True, timeout=30)
            print("[clash] mihomo 已重启")
        except Exception as e:
            print(f"[clash] 重启失败（请手动）: {e}", file=sys.stderr)
            return 1
    return 0


def check(proxy=None, timeout=10):
    print(f"[clash] config={CONFIG} exists={os.path.exists(CONFIG)}")
    print(f"[clash] subscription_file={SUB_FILE} "
          f"exists={os.path.exists(SUB_FILE)}")
    proxy = proxy or PROXY
    for name, url in (("google", "https://www.google.com/generate_204"),
                      ("x", "https://x.com/robots.txt")):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            with opener.open(req, timeout=timeout) as r:
                print(f"[clash] {name}: ok {getattr(r, 'status', 200)}")
        except Exception as e:
            print(f"[clash] {name}: fail {type(e).__name__}: {e}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="更新 Clash/mihomo 订阅")
    p.add_argument("--check", action="store_true", help="只检查配置与连通性")
    p.add_argument("--no-restart", action="store_true", help="更新后不重启")
    p.add_argument("--proxy", default="", help="下载订阅用的代理")
    args = p.parse_args(argv)
    if args.check:
        return check(proxy=args.proxy or None)
    return update(no_restart=args.no_restart, proxy=args.proxy or None)


if __name__ == "__main__":
    raise SystemExit(main())
