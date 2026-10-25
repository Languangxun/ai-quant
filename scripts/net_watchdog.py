#!/usr/bin/env python3
"""网络看门狗: 网线不通自动切 WiFi, 网线恢复自动切回.

背景 (2026-10-21): Pi 网口硬件故障 —— carrier 一直 up 但 IP 包完全不通
(ARP 都解析不到网关), 且每天 26 次 "Link is Down"。同时华为路由会屏蔽
本机 ICMP echo, 且对 TCP 有丢包, 所以探测方式必须绕开这两个坑。

策略:
1. 探测"默认路由实际走的网卡"能否 HTTPS 出网 (最贴近真实可用性);
2. 网线接口单独探 (绑定 SO_BINDTODEVICE) 判断网线是否可用;
3. 网线不可用 -> WiFi 主用 (route-metric 50) 且关掉网线自动连接;
4. 网线恢复 -> 切回网线主用 (route-metric 100);
5. WiFi 也不通 -> 重连一次 WiFi (清路由器侧 ARP/MAC 残留)。

本脚本需 root (SO_BINDTODEVICE), cron 里用 sudo 跑。
"""
import os
import socket
import subprocess
import sys
import time

GW = "192.168.3.1"
PROBE = ("pypi.tuna.tsinghua.edu.cn", 443)
ETH_CON = "Wired connection 1"
WIFI_CON = "WiFi-Fallback"
LOG = "/home/orangepi/setup.log"
STATE = "/home/orangepi/.net_route_mode"


def say(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s [net] %s\n" % (time.strftime("%F %T"), msg))
    except OSError:
        pass


def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception:
        return 1, ""


def https_ok(host, port, iface=None, timeout=6.0):
    """TCP 连通性探测 (可选绑定网卡)。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        if iface:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                         (iface + "\0").encode())
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()
    except Exception:
        return False


def default_iface():
    rc, out = run("ip route get 1.1.1.1 2>/dev/null | "
                  "awk '{for(i=1;i<=NF;i++) if($i==\"dev\") print $(i+1)}'")
    return out.strip().split()[0] if out.strip() else ""


def default_path_ok():
    """默认路由这条路能否出网 (连续 2 次, 容忍路由器丢包)。"""
    for _ in range(2):
        if https_ok(*PROBE):
            return True
        time.sleep(1)
    return False


def iface_ok(iface):
    """该网卡能否直达外网 (绑定网卡, 需 root)。"""
    if os.geteuid() != 0:
        return False
    for _ in range(2):
        if https_ok(PROBE[0], PROBE[1], iface=iface):
            return True
        time.sleep(1)
    return False


def set_metrics(eth_metric, wifi_metric):
    run('nmcli con mod "%s" ipv4.route-metric %d ipv6.route-metric %d'
        % (ETH_CON, eth_metric, eth_metric))
    run('nmcli con mod "%s" ipv4.route-metric %d ipv6.route-metric %d'
        % (WIFI_CON, wifi_metric, wifi_metric))
    run("nmcli dev reapply end0 >/dev/null 2>&1")
    run("nmcli dev reapply wlan0 >/dev/null 2>&1")


def set_eth_autoconnect(on):
    run('nmcli con mod "%s" connection.autoconnect %s'
        % (ETH_CON, "yes" if on else "no"))


def current_mode():
    try:
        with open(STATE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_mode(mode):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            f.write(mode)
    except OSError:
        pass


def bounce_wifi():
    say("WiFi 出网不通, 重连 WiFi")
    run('nmcli con down "%s" >/dev/null 2>&1' % WIFI_CON, timeout=25)
    time.sleep(3)
    run('nmcli con up "%s" >/dev/null 2>&1' % WIFI_CON, timeout=40)
    time.sleep(8)


def main():
    mode = current_mode()
    if not default_path_ok():
        say("默认路由出网不通, 尝试重连 WiFi")
        bounce_wifi()
        if not default_path_ok():
            say("重连后仍不通, 保持现状")
            return 1
        say("重连 WiFi 后已恢复")
    eth_ok = iface_ok("end0")
    wifi_ok = iface_ok("wlan0")
    if eth_ok and not wifi_ok:
        if mode != "eth":
            set_metrics(100, 600)
            set_eth_autoconnect(True)
            save_mode("eth")
            say("WiFi 不通, 切回网线主用 (eth=100 wifi=600)")
        else:
            say("WiFi 不通, 维持网线主用")
    elif not eth_ok and wifi_ok:
        if mode != "wifi":
            set_metrics(900, 50)
            set_eth_autoconnect(False)
            save_mode("wifi")
            say("网线不通, 切到 WiFi 主用 (eth=900 wifi=50, 网线自动连接已关)")
        else:
            say("网线不通, 维持 WiFi 主用")
    elif eth_ok and wifi_ok:
        if mode != "eth":
            set_metrics(100, 600)
            set_eth_autoconnect(True)
            save_mode("eth")
            say("网线已恢复, 切回网线主用 (eth=100 wifi=600)")
        else:
            say("网线正常, 维持网线主用")
    else:
        say("网线与 WiFi 均不可用, 保持现状 (等硬件恢复)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
