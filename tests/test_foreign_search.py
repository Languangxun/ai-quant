"""独立国外搜索程序的解析测试（不联网）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import foreign_search as fs


def test_unwrap_bing_base64():
    # a1 + base64("https://example.com/x")
    url = ("https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxl"
           "LmNvbS94&ntb=1")
    assert fs._clean_url(url) == "https://example.com/x"


def test_parse_duckduckgo_html():
    raw = '''
    <a rel="nofollow" class="result__a" href="https://example.com/a">Title A</a>
    <a class="result__snippet">Summary A</a>
    <a rel="nofollow" class="result__a" href="https://example.com/b">Title B</a>
    <a class="result__snippet">Summary B</a>
    '''
    rows = fs._parse_duckduckgo(raw)
    assert [r["url"] for r in rows] == ["https://example.com/a",
                                        "https://example.com/b"]
    assert rows[0]["summary"] == "Summary A"


def test_parse_bing_html():
    raw = '''
    <li class="b_algo"><h2><a href="https://example.com/a">Title A</a></h2>
    <p>Summary A</p></li>
    '''
    rows = fs._parse_bing(raw)
    assert rows and rows[0]["url"] == "https://example.com/a"
    assert rows[0]["summary"] == "Summary A"


def test_parse_ddg_lite():
    raw = '''<a href="https://example.com/a" class="result-link">Title A</a>'''
    rows = fs._parse_ddg_lite(raw)
    assert rows and rows[0]["url"] == "https://example.com/a"


def test_parse_google_news_rss():
    raw = b'''<rss><channel><item>
    <title>Trump tariff talks resume</title>
    <link>https://example.com/a</link>
    <pubDate>Wed, 05 Nov 2026 10:00:00 GMT</pubDate>
    <description>Summary A</description>
    </item></channel></rss>'''
    rows = fs._parse_google_news(raw)
    assert rows and rows[0]["engine"] == "google-news"
    assert rows[0]["date"] == "2026-11-05"


def test_search_site_x_filters_non_status(monkeypatch):
    monkeypatch.setattr(fs, "_fetch", lambda *a, **k: "raw")
    monkeypatch.setattr(fs, "_parse", lambda engine, raw: [
        {"title": "real", "url": "https://x.com/u/status/1", "summary": "s"},
        {"title": "help", "url": "https://x.com/help", "summary": "s"},
    ])
    rows = fs.search("Trump", engines=["duckduckgo"], count=5, site="x")
    assert [r["url"] for r in rows] == ["https://x.com/u/status/1"]
