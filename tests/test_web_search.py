"""DeepSeek Search API 自动联网检索的单元测试（不发起真实请求）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.web_search import WebSearchClient, build_queries


def test_build_queries_stock_context():
    ctx = {
        "date": "2026-11-04",
        "mode": "stock",
        "candidates": [
            {"code": "sz000725", "name": "京东方A"},
            {"code": "sh600519", "name": "贵州茅台"},
        ],
        "positions": [{"code": "sh601318", "name": "中国平安"}],
    }
    qs = build_queries(ctx, 2)
    assert len(qs) == 2
    assert all(len(q) <= 128 for q in qs)
    assert "京东方A" in qs[0] and "贵州茅台" in qs[0]
    assert "中国平安" in qs[0]
    assert "after:2026-11-01" in qs[0]
    assert "A股" in qs[1] and "after:2026-11-01" in qs[1]


def test_build_queries_fund_context():
    ctx = {
        "date": "2026-11-04",
        "market": [{"symbol": "半导体"}, {"symbol": "通信"}],
    }
    qs = build_queries(ctx, 2)
    assert len(qs) == 2
    assert "半导体" in qs[0] and "通信" in qs[0]
    assert "ETF" in qs[0]


def test_build_queries_unknown_context_empty():
    assert build_queries({"foo": "bar"}) == []


def test_query_truncated_to_limit():
    ctx = {
        "date": "2026-11-04",
        "candidates": [{"name": "超长名称" * 40}],
    }
    qs = build_queries(ctx, 1)
    assert qs and len(qs[0]) <= 128
    assert "after:" in qs[0]


class _Resp:
    status_code = 200

    def __init__(self, results):
        self._results = results

    def json(self):
        return {"results": self._results}


class _Session:
    def __init__(self, results=None, error=None):
        self.results = results or [{"title": "t", "url": "https://x",
                                    "summary": "s", "site_name": "site"}]
        self.error = error
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json)
        if self.error:
            raise self.error
        return _Resp(self.results)


def _client(tmp_path, session, **kw):
    return WebSearchClient(api_key="k", cache_path=str(tmp_path / "c.json"),
                           retry_wait=0, session=session, **kw)


def test_search_uses_cache(tmp_path):
    s1 = _Session()
    c1 = _client(tmp_path, s1)
    a = c1.search("A股 行情")
    b = c1.search("A股 行情")
    assert a == b and len(s1.calls) == 1
    # 新实例读同一缓存文件，仍不重复请求
    s2 = _Session()
    c2 = _client(tmp_path, s2)
    assert c2.search("A股 行情") == a
    assert s2.calls == []


def test_search_failure_returns_empty(tmp_path):
    c = _client(tmp_path, _Session(error=RuntimeError("boom")))
    assert c.search("A股 行情") == []


def test_gather_dedupes_and_limits_chars(tmp_path):
    results = [
        {"title": "a", "url": "https://a", "summary": "x" * 100},
        {"title": "b", "url": "https://b", "summary": "y" * 100},
        {"title": "a2", "url": "https://a", "summary": "dup" * 50},
    ]
    c = _client(tmp_path, _Session(results=results), max_chars=80)
    out = c.gather({"date": "2026-11-04", "mode": "stock",
                    "candidates": [{"name": "京东方A"}]})
    assert len(out) == 1 and out[0]["url"] == "https://a"


def test_gather_includes_event_watch_first(tmp_path):
    results = [{"title": "特朗普关税升级 对A股影响", "url": "https://e/1",
                "summary": "关税上调，出口链承压", "date": "2026-11-05",
                "site_name": "财联社"}]
    c = _client(tmp_path, _Session(results=results),
                event_topics=("trump",), event_max_queries=1,
                event_days=3, event_limit=3)
    out = c.gather({"date": "2026-11-06", "mode": "stock",
                    "candidates": [{"name": "京东方A"}]})
    assert c.last_event and c.last_event["items"]
    assert out and out[0]["title"].startswith("特朗普")
    assert c.last_event["queries"][0].endswith("lang:zh")


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _CompletionResp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return _CompletionResp(self.content)


class _FakeSearch:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.contexts = []

    def gather(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.results


def _make_model(tmp_path, search):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    from models.deepseek import DeepSeekModel
    m = DeepSeekModel(system_prompt="SYS", context_dir=str(tmp_path),
                      search_client=search)
    m.client = type("C", (), {})()
    m.client.chat = type("S", (), {})()
    m.client.chat.completions = _FakeCompletions(
        '{"market_view":"ok","orders":[]}')
    return m


def test_model_injects_web_search(tmp_path):
    fake = _FakeSearch([{"title": "利好", "url": "https://n",
                         "summary": "政策支持", "date": "2026-11-03"}])
    m = _make_model(tmp_path, fake)
    out = m.analyze({"date": "2026-11-04", "mode": "stock",
                     "candidates": [{"code": "sz000725"}]})
    assert out["market_view"] == "ok"
    assert fake.contexts  # 自动调用
    payload = json.loads(
        m.client.chat.completions.calls[0]["messages"][-1]["content"])
    assert payload["web_search"]["results"][0]["title"] == "利好"
    assert "指令" in payload["web_search"]["note"]


def test_model_search_failure_still_decides(tmp_path):
    m = _make_model(tmp_path, _FakeSearch(error=RuntimeError("network")))
    out = m.analyze({"date": "2026-11-04", "mode": "stock",
                     "candidates": [{"code": "sz000725"}]})
    assert out["market_view"] == "ok"
    payload = json.loads(
        m.client.chat.completions.calls[0]["messages"][-1]["content"])
    assert "web_search" not in payload


def test_model_search_disabled(tmp_path):
    m = _make_model(tmp_path, False)
    m.analyze({"date": "2026-11-04", "mode": "stock",
               "candidates": [{"code": "sz000725"}]})
    payload = json.loads(
        m.client.chat.completions.calls[0]["messages"][-1]["content"])
    assert "web_search" not in payload


def test_build_from_config(monkeypatch):
    from models.web_search import WebSearchClient, build_from_config
    monkeypatch.delenv("DEEPSEEK_SEARCH", raising=False)
    cfg = {"models": {"search": {"enabled": True, "count": 3}}}
    client = build_from_config(cfg, api_key="k")
    assert isinstance(client, WebSearchClient)
    assert client.count == 3
    cfg["models"]["search"]["event_watch"] = {
        "enabled": True, "topics": ["trump"], "days": 2}
    client = build_from_config(cfg, api_key="k")
    assert client.event_topics == ["trump"] and client.event_days == 2
    cfg["models"]["search"]["enabled"] = False
    assert build_from_config(cfg, api_key="k") is None
    monkeypatch.setenv("DEEPSEEK_SEARCH", "0")
    cfg["models"]["search"]["enabled"] = True
    assert build_from_config(cfg, api_key="k") is None
