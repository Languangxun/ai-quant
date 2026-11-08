#!/usr/bin/env python3
"""国外搜索（独立程序，纯标准库）：DuckDuckGo / Bing / Google + X 站内检索。

用途：
- ai-quant 盘前/盘中事件检索的“外网补充源”，查 Google、X（Twitter）等；
- 也可单独当命令行工具用：
    python3 scripts/foreign_search.py -q "Trump tariff China" --json
    python3 scripts/foreign_search.py -q "Trump tariff" --site x --count 10
    python3 scripts/foreign_search.py --check

代理：
- 优先 FOREIGN_SEARCH_PROXY
- 其次 HTTPS_PROXY / HTTP_PROXY
- 最后默认 http://127.0.0.1:7890（Pi 上 Clash/mihomo 的 mixed-port）
不会把代理地址或订阅写进仓库；Pi 的实际配置放
~/.config/ai-quant/foreign_search.env（chmod 600）。

注意：Google/Bing/DDG 的 HTML 结构会变，解析失败会自动降级到下一个
引擎；任何异常都返回空列表，不影响主流程。
"""
import argparse
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser(
    "~/.config/ai-quant/foreign_search.env")
DEFAULT_PROXY = "http://127.0.0.1:7890"
DEFAULT_ENGINES = ("google-news", "bing", "duckduckgo", "wsj", "investing")

# 英文财经 RSS 源（免费、无需 key；经代理抓取）
RSS_SOURCES = {
    "wsj": [
        ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
         "WSJ Markets"),
        ("https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
         "WSJ World"),
        ("https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
         "WSJ Business"),
        ("https://feeds.content.dowjones.io/public/rss/RSSWSJD", "WSJ Tech"),
    ],
    "investing": [
        ("https://www.investing.com/rss/news.rss", "Investing All"),
        ("https://www.investing.com/rss/news_25.rss", "Investing Stocks"),
        ("https://www.investing.com/rss/news_14.rss", "Investing Forex"),
        ("https://www.investing.com/rss/news_11.rss", "Investing Crypto"),
        ("https://www.investing.com/rss/news_20.rss", "Investing Commodities"),
    ],
    "cnbc": [("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC Top"),
             ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "CNBC Markets")],
    "marketwatch": [("https://feeds.content.dowjones.io/public/rss/mw_topstories",
                     "MarketWatch Top")],
}
DEFAULT_TIMEOUT = 10.0
DEFAULT_COUNT = 8
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_config():
    """读取本地 env（KEY=VALUE），缺项回退环境变量/默认值。"""
    conf = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    env = os.environ
    proxy = (conf.get("FOREIGN_SEARCH_PROXY")
             or env.get("FOREIGN_SEARCH_PROXY")
             or env.get("HTTPS_PROXY") or env.get("https_proxy")
             or env.get("HTTP_PROXY") or env.get("http_proxy")
             or DEFAULT_PROXY)
    engines = (conf.get("FOREIGN_SEARCH_ENGINES")
               or env.get("FOREIGN_SEARCH_ENGINES")
               or ",".join(DEFAULT_ENGINES))
    try:
        timeout = float(conf.get("FOREIGN_SEARCH_TIMEOUT")
                        or env.get("FOREIGN_SEARCH_TIMEOUT")
                        or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    try:
        count = int(conf.get("FOREIGN_SEARCH_COUNT")
                    or env.get("FOREIGN_SEARCH_COUNT")
                    or DEFAULT_COUNT)
    except ValueError:
        count = DEFAULT_COUNT
    token = (conf.get("X_BEARER_TOKEN")
             or env.get("X_BEARER_TOKEN")
             or env.get("TWITTER_BEARER_TOKEN") or "")
    return {
        "proxy": proxy,
        "engines": [x.strip().lower() for x in engines.split(",") if x.strip()],
        "timeout": max(2.0, timeout),
        "count": max(1, min(30, count)),
        "x_token": token.strip(),
    }


def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _fetch(url, proxy, timeout, headers=None):
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        "Cache-Control": "no-cache",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with _opener(proxy).open(req, timeout=timeout) as resp:
        raw = resp.read(2500000)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, "replace")


def _text(raw):
    text = re.sub(r"<script[^>]*>.*?</script>", " ", raw,
                  flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _unwrap_ddg(url):
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            return (qs.get("uddg") or [""])[0]
    except Exception:
        pass
    return url


def _unwrap_bing(url):
    """Bing 的 /ck/a?...&u=a1<base64> 跳转链接还原真实地址。"""
    try:
        parsed = urllib.parse.urlparse(url)
        if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
            return url
        u = (urllib.parse.parse_qs(parsed.query).get("u") or [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            raw += "=" * (-len(raw) % 4)
            import base64
            return base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:
        pass
    return url


def _clean_url(url):
    url = html.unescape(str(url or "")).strip()
    if url.startswith("/url?q="):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        url = (qs.get("q") or [""])[0]
    if url.startswith("//"):
        url = "https:" + url
    return _unwrap_bing(url)


def _parse_duckduckgo(raw):
    out = []
    # 结果块：<a class="result__a" href="...">标题</a> ... snippet
    for m in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>'
            r'(.*?)</a>(.*?)(?=<a[^>]+class="[^"]*result__a|$)',
            raw, flags=re.S | re.I):
        url = _clean_url(_unwrap_ddg(html.unescape(m.group(1))))
        title = _text(m.group(2))
        block = m.group(3) or ""
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</',
                       block, flags=re.S | re.I)
        summary = _text(sm.group(1)) if sm else _text(block)[:300]
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "summary": summary,
                        "engine": "duckduckgo"})
    return out


def _parse_ddg_lite(raw):
    """lite.duckduckgo.com 的表格结构，作为 html 版失败时的兜底。"""
    out = []
    for m in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*class=[\'"][^\'"]*result-link[^\'"]*'
            r'[\'"][^>]*>(.*?)</a>', raw, flags=re.S | re.I):
        url = _clean_url(_unwrap_ddg(html.unescape(m.group(1))))
        title = _text(m.group(2))
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "summary": "",
                        "engine": "duckduckgo"})
    return out


def _parse_bing(raw):
    out = []
    for m in re.finditer(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                         raw, flags=re.S | re.I):
        block = m.group(1)
        am = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                       block, flags=re.S | re.I)
        if not am:
            continue
        url = _clean_url(am.group(1))
        title = _text(am.group(2))
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, flags=re.S | re.I)
        summary = _text(sm.group(1)) if sm else _text(block)[:300]
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "summary": summary,
                        "engine": "bing"})
    return out


def _parse_google(raw):
    out = []
    for m in re.finditer(
            r'<a[^>]+href="(/url\?q=[^"]+)"[^>]*>(.*?)</a>', raw,
            flags=re.S | re.I):
        url = _clean_url(m.group(1))
        title = _text(m.group(2))
        if not title or len(title) < 4 or not url.startswith("http"):
            continue
        out.append({"title": title, "url": url, "summary": "",
                    "engine": "google"})
    return out


def _engine_url(engine, query, count, days=7):
    if engine == "google-news":
        # Google News RSS：稳定、带发布时间，适合外网财经事件
        return ("https://news.google.com/rss/search?q="
                + urllib.parse.quote_plus(
                    f"{query} when:{max(1, min(30, days))}d")
                + "&hl=en-US&gl=US&ceid=US:en")
    if engine == "duckduckgo":
        df = "d" if days <= 1 else "w" if days <= 7 else "m"
        return ("https://html.duckduckgo.com/html/?q="
                + urllib.parse.quote_plus(query) + f"&df={df}")
    if engine == "bing":
        return ("https://www.bing.com/search?q="
                + urllib.parse.quote_plus(query)
                + f"&count={max(10, count)}&setlang=en")
    if engine == "google":
        qdr = "d" if days <= 1 else "w" if days <= 7 else "m"
        return ("https://www.google.com/search?q="
                + urllib.parse.quote_plus(query)
                + f"&num={max(10, count)}&hl=en&gbv=1&tbs=qdr:{qdr}")
    return ""


def _parse_rss_feed(raw, engine, source_name=""):
    """通用 RSS 2.0 解析（WSJ/Investing/CNBC/MarketWatch 共用）。"""
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return out
    for item in root.iter("item"):
        title = _text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _text(item.findtext("description") or "")
        pub = (item.findtext("pubDate") or "").strip()
        date = ""
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
        if m:
            try:
                from datetime import datetime as _dt
                date = _dt.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}",
                    "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                date = ""
        if not date:
            from datetime import datetime as _dt
            for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    date = _dt.strptime(pub, fmt).strftime("%Y-%m-%d")
                    break
                except (ValueError, TypeError):
                    continue
        if title and link.startswith("http"):
            out.append({"title": title, "url": link,
                        "summary": desc[:600], "date": date,
                        "site": source_name or engine, "engine": engine})
    return out


def _fetch_rss_engine(engine, proxy, timeout, per_feed=6):
    """抓取 RSS 源组（wsj/investing/cnbc/marketwatch），按时间排序。"""
    feeds = RSS_SOURCES.get(engine) or []
    out = []
    for url, name in feeds:
        try:
            raw = _fetch(url, proxy, timeout)
            rows = _parse_rss_feed(raw, engine, name)
        except Exception as e:
            print(f"[foreign_search] {engine}({name}) 失败: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            rows = []
        rows.sort(key=lambda x: x.get("date") or "", reverse=True)
        out.extend(rows[:per_feed])
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


def _parse_google_news(raw):
    """Google News RSS（XML）解析；比抓 Google 搜索结果稳定得多。"""
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return out
    for item in root.iter("item"):
        title = _text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _text(item.findtext("description") or "")
        source = _text(item.findtext("source") or "")
        pub = (item.findtext("pubDate") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[:-len(source) - 3].strip()
        if desc and source and desc.endswith(source):
            desc = desc[:-len(source)].strip()
        date = ""
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
        if m:
            try:
                from datetime import datetime as _dt
                date = _dt.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3)}",
                    "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                date = ""
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "summary": desc[:600],
                        "date": date, "site": source[:40],
                        "engine": "google-news"})
    return out


def _parse(engine, raw):
    if engine == "google-news":
        return _parse_google_news(raw)
    if engine == "duckduckgo":
        return _parse_duckduckgo(raw)
    if engine == "bing":
        return _parse_bing(raw)
    if engine == "google":
        return _parse_google(raw)
    return []


def _x_api_search(query, count, proxy, timeout, days, token):
    """X 官方 API v2 recent search；有 Bearer Token 时优先使用。"""
    if not token:
        return []
    import datetime as _dt
    try:
        start = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(days=max(1, int(days))))
        params = {
            "query": f"{query} -is:retweet",
            "max_results": max(10, min(100, int(count) * 2)),
            "tweet.fields": "created_at,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "username,name",
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        url = ("https://api.x.com/2/tweets/search/recent?"
               + urllib.parse.urlencode(params))
        raw = _fetch(url, proxy, timeout,
                     headers={"Authorization": f"Bearer {token}"})
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        print(f"[foreign_search] X API 失败: {type(e).__name__}: {e}",
              file=sys.stderr)
        return []
    users = {}
    for u in ((data.get("includes") or {}).get("users") or []):
        users[str(u.get("id"))] = u.get("username") or ""
    out = []
    for tw in data.get("data") or []:
        text = re.sub(r"\s+", " ", str(tw.get("text") or "")).strip()
        if not text:
            continue
        user = users.get(str(tw.get("author_id"))) or "i"
        out.append({
            "title": text[:160],
            "url": f"https://x.com/{user}/status/{tw.get('id')}",
            "summary": text[:600],
            "date": str(tw.get("created_at") or "")[:10],
            "engine": "x-api",
            "site": "x.com",
        })
    return out[:count]


def search(query, engines=None, count=DEFAULT_COUNT, proxy=None,
           timeout=DEFAULT_TIMEOUT, site="", days=7):
    """多引擎搜索；返回 [{title,url,summary,engine,query}]，失败返回 []。"""
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not query:
        return []
    x_site = site in ("x", "twitter")
    if x_site:
        query = f"site:x.com OR site:twitter.com {query}"
    elif site:
        query = f"site:{site} {query}"
    conf = load_config()
    engines = engines or conf["engines"]
    proxy = proxy if proxy is not None else conf["proxy"]
    timeout = float(timeout or conf["timeout"])
    count = max(1, min(30, int(count or conf["count"])))
    days = max(1, min(365, int(days or 7)))
    if x_site and conf.get("x_token"):
        return _x_api_search(query, count, proxy, timeout, days,
                             conf["x_token"])
    if x_site:
        # Google News 不索引 X 帖子；X 站内检索交给 DDG/Bing 的 site:
        engines = [e for e in engines
                   if e not in ("google-news", "google")] or ["duckduckgo"]
    seen, out = set(), []
    for engine in engines:
        engine = str(engine or "").strip().lower()
        # RSS 英文财经源组：直接抓最新新闻流
        if engine in RSS_SOURCES and not x_site:
            for row in _fetch_rss_engine(engine, proxy, timeout,
                                         per_feed=max(3, count // 2)):
                key = re.sub(r"[?#].*$", "", row.get("url") or "").lower()
                if key and key not in seen:
                    seen.add(key)
                    out.append(row)
            continue
        url = _engine_url(engine, query, count, days)
        if not url:
            continue
        rows = []
        for attempt in range(2):
            try:
                raw = _fetch(url, proxy, timeout)
                rows = _parse(engine, raw)
            except Exception as e:
                print(f"[foreign_search] {engine} 失败"
                      f"({attempt + 1}/2): {type(e).__name__}: {e}",
                      file=sys.stderr)
                rows = []
            if rows:
                break
            time.sleep(0.6)
        # DuckDuckGo html 偶尔返回空/风控页，lite 版通常仍可用
        if engine == "duckduckgo" and not rows:
            try:
                lite = ("https://lite.duckduckgo.com/lite/?q="
                        + urllib.parse.quote_plus(query)
                        + ("&df=d" if days <= 1 else
                           "&df=w" if days <= 7 else "&df=m"))
                rows = _parse_ddg_lite(_fetch(lite, proxy, timeout))
            except Exception as e:
                print(f"[foreign_search] duckduckgo-lite 失败: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                rows = []
        for row in rows:
            link = str(row.get("url") or "")
            if x_site and "/status/" not in link.lower():
                continue
            key = re.sub(r"[?#].*$", "", link).lower().rstrip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            row["query"] = query
            out.append(row)
            if len(out) >= count:
                return out
        time.sleep(0.2)
    return out


def check(proxy=None, timeout=10.0):
    """连通性检查：Google、X、DuckDuckGo。返回 {name: (ok, status)}."""
    conf = load_config()
    proxy = proxy if proxy is not None else conf["proxy"]
    targets = {
        "google": "https://www.google.com/generate_204",
        "x": "https://x.com/robots.txt",
        "duckduckgo": "https://duckduckgo.com/robots.txt",
        "wsj": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "investing": "https://www.investing.com/rss/news.rss",
    }
    out = {}
    for name, url in targets.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with _opener(proxy).open(req, timeout=timeout) as r:
                out[name] = (True, getattr(r, "status", 200))
        except Exception as e:
            out[name] = (False, f"{type(e).__name__}: {e}"[:120])
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="国外搜索（DuckDuckGo/Bing/Google/X）")
    p.add_argument("-q", "--query", default="", help="搜索词")
    p.add_argument("--engines", default="",
                   help="逗号分隔：duckduckgo,bing,google（默认读本地配置）")
    p.add_argument("--site", default="",
                   help="限定站点：x/twitter 或域名，如 reuters.com")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT)
    p.add_argument("--days", type=int, default=7,
                   help="时效：1=近一天，7=近一周，30=近一月")
    p.add_argument("--proxy", default="", help="覆盖代理，如 http://127.0.0.1:7890")
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--json", action="store_true", help="JSON 输出（默认就是）")
    p.add_argument("--check", action="store_true", help="只检查代理/站点连通性")
    args = p.parse_args(argv)

    if args.check:
        res = check(proxy=args.proxy or None,
                    timeout=args.timeout or DEFAULT_TIMEOUT)
        print(json.dumps({k: {"ok": v[0], "detail": v[1]}
                          for k, v in res.items()}, ensure_ascii=False, indent=2))
        return 0 if all(v[0] for v in res.values()) else 2

    engines = [x.strip() for x in args.engines.split(",") if x.strip()] or None
    rows = search(
        args.query,
        engines=engines,
        count=args.count,
        proxy=args.proxy or None,
        timeout=args.timeout or None,
        site=args.site,
        days=args.days,
    )
    print(json.dumps({"query": args.query, "site": args.site,
                      "count": len(rows), "results": rows},
                     ensure_ascii=False, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
