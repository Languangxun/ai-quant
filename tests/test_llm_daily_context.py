"""LLM 一天一个会话 (前缀缓存优化) 的单元测试, mock API 不发真实请求."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    def __init__(self, hit, miss, comp):
        self.prompt_cache_hit_tokens = hit
        self.prompt_cache_miss_tokens = miss
        self.completion_tokens = comp


class _Resp:
    def __init__(self, content, hit=0, miss=100, comp=20):
        self.choices = [_Choice(content)]
        self.usage = _Usage(hit, miss, comp)


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _make_model(tmpdir, responses):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    from models.deepseek import DeepSeekModel
    m = DeepSeekModel(system_prompt="SYS", daily_context=True,
                      context_dir=str(tmpdir))
    m.client = type("C", (), {})()
    m.client.chat = type("S", (), {})()
    m.client.chat.completions = _FakeCompletions(responses)
    return m


def test_daily_context_accumulates_turns(tmp_path):
    m = _make_model(tmp_path, [
        _Resp('{"market_view":"a","orders":[]}', 10, 500),
        _Resp('{"market_view":"b","orders":[]}', 480, 30),
    ])
    m.analyze({"session": "10:00", "x": 1})
    m.analyze({"session": "11:00", "x": 2})
    # 第二次请求: system + 第一轮(user, assistant) + 新 user
    msgs = m.client.chat.completions.calls[1]["messages"]
    assert [mm["role"] for mm in msgs] == ["system", "user", "assistant",
                                           "user"]
    assert msgs[0]["content"] == "SYS"
    assert json.loads(msgs[1]["content"]) == {"session": "10:00", "x": 1}
    assert msgs[2]["content"] == '{"market_view":"a","orders":[]}'
    assert json.loads(msgs[3]["content"]) == {"session": "11:00", "x": 2}
    # 持久化: 当日文件 2 轮 + usage 记录
    day = time.strftime("%Y-%m-%d")
    doc = json.load(open(tmp_path / f"llm_daily_{day}.json",
                         encoding="utf-8"))
    assert len(doc["turns"]) == 2
    assert doc["usage"][1]["cache_hit"] == 480


def test_parse_failure_not_appended(tmp_path):
    # 第一次返回垃圾(解析失败), 重试成功: 失败轮不进历史, 前缀保持不变
    m = _make_model(tmp_path, [
        _Resp("not json at all"),
        _Resp('{"market_view":"ok","orders":[]}'),
    ])
    m.analyze({"session": "10:00"})
    calls = m.client.chat.completions.calls
    assert len(calls) == 2
    assert [mm["role"] for mm in calls[1]["messages"]] == ["system", "user"]
    day = time.strftime("%Y-%m-%d")
    doc = json.load(open(tmp_path / f"llm_daily_{day}.json",
                         encoding="utf-8"))
    assert len(doc["turns"]) == 1


def test_day_rollover_fresh_session(tmp_path):
    day = time.strftime("%Y-%m-%d")
    # 用 2 天前 (在 7 天 GC 窗口内, 文件应保留; 当天之外即开新会话)
    old_day = time.strftime("%Y-%m-%d",
                            time.gmtime(time.time() - 2 * 86400))
    p = tmp_path / f"llm_daily_{old_day}.json"
    p.write_text(json.dumps({"date": old_day, "turns": [
        {"user": "u0", "assistant": "a0"}], "usage": []}), encoding="utf-8")
    m = _make_model(tmp_path, [_Resp('{"market_view":"n","orders":[]}')])
    m.analyze({"session": "10:00"})
    # 昨天文件原样保留, 今天从空会话开始
    assert p.exists()
    calls = m.client.chat.completions.calls
    assert [mm["role"] for mm in calls[0]["messages"]] == ["system", "user"]
    doc = json.load(open(tmp_path / f"llm_daily_{day}.json",
                         encoding="utf-8"))
    assert len(doc["turns"]) == 1


def test_daily_context_off_by_default(tmp_path):
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    from models.deepseek import DeepSeekModel
    m = DeepSeekModel(system_prompt="SYS", context_dir=str(tmp_path))
    m.client = type("C", (), {})()
    m.client.chat = type("S", (), {})()
    m.client.chat.completions = _FakeCompletions(
        [_Resp('{"market_view":"x","orders":[]}')])
    m.analyze({"a": 1})
    msgs = m.client.chat.completions.calls[0]["messages"]
    assert [mm["role"] for mm in msgs] == ["system", "user"]  # 单轮不变
    assert not list(tmp_path.glob("llm_daily_*.json"))         # 不落盘
