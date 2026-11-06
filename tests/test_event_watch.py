"""重大事件检索（特朗普/关税/地缘）的单元测试：不联网。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import event_watch


def test_build_queries_has_recency_and_bilingual():
    qs = event_watch.build_queries("2026-11-06", days=3,
                                   topics=("trump", "macro"),
                                   max_queries=3)
    assert len(qs) == 3
    assert all(len(q) <= 128 for q in qs)
    assert all("after:2026-11-03" in q for q in qs)
    assert any("lang:en" in q for q in qs)
    assert any("特朗普" in q for q in qs)


def test_focus_query_comes_first():
    qs = event_watch.build_queries("2026-11-06", days=2,
                                   topics=("trump",), max_queries=2,
                                   focus="美国中期选举 关税")
    assert "美国中期选举" in qs[0]
    assert len(qs) == 2


def test_rank_dedupes_syndicated_copies_and_prefers_authority():
    items = [
        {"title": "特朗普宣布新关税措施_东方财富网", "url": "https://e/1",
         "summary": "关税升级，出口链承压", "date": "2026-11-05",
         "site": "东方财富"},
        {"title": "特朗普宣布新关税措施", "url": "https://r/1",
         "summary": "关税升级，出口链承压", "date": "2026-11-05",
         "site": "Reuters"},
        {"title": "隔夜美股小幅收涨", "url": "https://x/2",
         "summary": "纳指涨0.5%", "date": "2026-11-03", "site": "新浪财经"},
    ]
    out = event_watch.rank(items, "2026-11-06", days=3, limit=5,
                           topics=("trump", "macro"))
    titles = [x["title"] for x in out]
    assert any("Reuters" == x["site"] for x in out)
    assert not any("东方财富" == x["site"] and "关税" in x["title"]
                   for x in out)
    assert len(titles) == len(set(titles))


def test_rank_drops_generic_aggregation_titles():
    items = [
        {"title": "相关资讯 :", "url": "https://junk/1",
         "summary": "特朗普 关税 中期选举 " * 20, "date": "2026-11-05",
         "site": "澎博财经"},
        {"title": "国际综合财经", "url": "https://junk/2",
         "summary": "特朗普 关税 美联储 " * 20, "date": "2026-11-05",
         "site": "威尔鑫"},
        {"title": "特朗普中期选举后关税政策走向", "url": "https://ok/1",
         "summary": "国会制约增强", "date": "2026-11-05",
         "site": "财联社"},
    ]
    out = event_watch.rank(items, "2026-11-06", days=3, limit=5,
                           topics=("trump",))
    assert [x["url"] for x in out] == ["https://ok/1"]


def test_rank_drops_stale_and_low_value():
    items = [
        {"title": "旧闻：特朗普关税", "url": "https://old",
         "summary": "x", "date": "2026-09-01", "site": "财联社"},
        {"title": "开户广告：加微信推荐股票", "url": "https://ad",
         "summary": "扫码", "date": "2026-11-05", "site": "股吧"},
        {"title": "特朗普关税豁免最新消息", "url": "https://new",
         "summary": "部分商品豁免", "date": "2026-11-05", "site": "财联社"},
    ]
    out = event_watch.rank(items, "2026-11-06", days=3, limit=5,
                           topics=("trump",))
    assert [x["url"] for x in out] == ["https://new"]


def test_rank_caps_two_items_per_site():
    items = [
        {"title": f"特朗普关税消息{i}", "url": f"https://g/{i}",
         "summary": "关税", "date": "2026-11-05", "site": "股吧"}
        for i in range(6)
    ]
    out = event_watch.rank(items, "2026-11-06", days=3, limit=6,
                           topics=("trump",))
    assert len(out) <= 2


def test_detect_signals_and_salience():
    items = [
        {"title": "美国宣布加征关税 贸易战升级",
         "summary": "对华关税上调，市场避险", "site": "财联社"},
        {"title": "特朗普关税升级引发抛售",
         "summary": "出口链承压", "site": "Reuters"},
    ]
    sig = event_watch.detect_signals(items)
    keys = {s["key"] for s in sig}
    assert "tariff_escalation" in keys
    assert event_watch.salience(items, sig) > 0
    text = event_watch.brief(items, days=3, signals=sig)
    assert "关税" in text and "事件信号" in text


class _Client:
    def __init__(self, results=None):
        self.queries = []
        self.results = results or []

    def search(self, query):
        self.queries.append(query)
        return list(self.results)


def test_collect_merges_foreign_results(monkeypatch):
    from scripts import foreign_search

    def fake_search(query, **kw):
        return [{"title": "Trump tariff update", "url": "https://x.com/a/status/1",
                 "summary": "new tariff", "engine": "duckduckgo"}]

    monkeypatch.setattr(foreign_search, "search", fake_search)
    c = _Client([
        {"title": "特朗普关税最新", "url": "https://a", "summary": "关税",
         "date": "2026-11-05", "site": "财联社"},
    ])
    doc = event_watch.collect(
        c, "2026-11-06", days=3, topics=("trump",), max_queries=1,
        limit=5,
        foreign={"enabled": True, "max_queries": 1, "count": 3,
                 "include_x": True, "timeout": 3, "days": 3})
    assert doc["foreign_count"] >= 1
    assert any(it.get("source") == "foreign" for it in doc["items"])


def test_collect_merges_and_dedupes():
    c = _Client([
        {"title": "特朗普关税最新", "url": "https://a", "summary": "关税",
         "date": "2026-11-05", "site": "财联社"},
        {"title": "特朗普关税最新_转载", "url": "https://b",
         "summary": "关税", "date": "2026-11-05", "site": "股吧"},
    ])
    doc = event_watch.collect(c, "2026-11-06", days=3,
                              topics=("trump",), max_queries=2, limit=5)
    assert len(c.queries) == 2
    assert doc["items"]
    assert doc["brief"]
    assert doc["note"]
    assert len(doc["items"]) == 1
