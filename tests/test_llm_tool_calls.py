"""DeepSeek 工具调用循环测试（mock API，不发真实请求）。"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message):
        self.message = message


class _Usage:
    def __init__(self):
        self.prompt_cache_hit_tokens = 10
        self.prompt_cache_miss_tokens = 20
        self.completion_tokens = 5


class _Resp:
    def __init__(self, content=None, tool_calls=None):
        self.choices = [_Choice(_Msg(content, tool_calls))]
        self.usage = _Usage()


class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Box:
    def __init__(self):
        self.trace = []
        self.search_client = None

    def specs(self):
        return [{"type": "function", "function": {
            "name": "get_market", "description": "d",
            "parameters": {"type": "object", "properties": {}}}}]

    def call(self, name, args):
        self.trace.append({"name": name, "args": args,
                           "elapsed": 0.01, "error": False})
        return '{"market":"ok"}'


def _model(tmp_path, responses, toolbox=None, daily=False, rounds=3):
    from models.deepseek import DeepSeekModel

    m = object.__new__(DeepSeekModel)
    m.model = "test"
    m.temperature = 0.2
    m.system_prompt = "SYS"
    m.daily_context = daily
    m.context_dir = str(tmp_path)
    m.search_client = None
    m.tools_enabled = True
    m.tool_max_rounds = rounds
    m.toolbox = toolbox
    m.tools_available = True
    m.last_tool_trace = []
    comp = _Completions(responses)
    m.client = type("C", (), {})()
    m.client.chat = type("S", (), {})()
    m.client.chat.completions = comp
    return m, comp


def _tool_round():
    return _Resp(tool_calls=[_ToolCall(
        "call_1", "get_market", '{"x": 1}')])


def test_tool_round_then_final_json(tmp_path):
    box = _Box()
    m, comp = _model(tmp_path, [
        _tool_round(),
        _Resp('{"market_view":"ok","orders":[]}'),
    ], toolbox=box)
    result = m._chat({"date": "2026-11-06"})
    assert result["market_view"] == "ok"
    assert len(comp.calls) == 2
    assert "tools" in comp.calls[0]
    second = comp.calls[1]["messages"]
    assert any(x.get("role") == "tool"
               and x.get("tool_call_id") == "call_1" for x in second)
    assert any(x.get("role") == "assistant" and x.get("tool_calls")
               for x in second)
    assert box.trace and box.trace[0]["name"] == "get_market"
    assert m.last_tool_trace == box.trace


def test_tool_messages_not_persisted_in_daily_history(tmp_path):
    box = _Box()
    m, comp = _model(tmp_path, [
        _tool_round(),
        _Resp('{"orders":[]}'),
    ], toolbox=box, daily=True)
    m._chat({"date": "2026-11-06"})
    path = os.path.join(
        str(tmp_path), f"llm_daily_{time.strftime('%Y-%m-%d')}.json")
    doc = json.load(open(path, encoding="utf-8"))
    assert len(doc["turns"]) == 1
    turn = doc["turns"][0]
    assert turn["assistant"] == '{"orders":[]}'
    assert "tool" not in turn["assistant"]
    assert "tool_calls" not in json.dumps(turn)


def test_tools_unsupported_falls_back(tmp_path):
    box = _Box()
    m, comp = _model(tmp_path, [
        RuntimeError("tools are not supported by this model"),
        _Resp('{"orders":[]}'),
    ], toolbox=box)
    result = m._chat({})
    assert result == {"orders": []}
    assert m.tools_available is False
    assert len(comp.calls) == 2
    assert "tools" in comp.calls[0]
    assert "tools" not in comp.calls[1]


def test_tool_round_limit_forces_final_answer(tmp_path):
    box = _Box()
    m, comp = _model(tmp_path, [
        _tool_round(),
        _Resp('{"orders":[]}'),
    ], toolbox=box, rounds=1)
    result = m._chat({})
    assert result == {"orders": []}
    assert len(comp.calls) == 2
    final_msgs = comp.calls[1]["messages"]
    assert "tools" not in comp.calls[1]
    assert final_msgs[-1]["role"] == "user"
    assert "上限" in final_msgs[-1]["content"]
    assert len(box.trace) == 1


def test_bad_tool_arguments_are_ignored(tmp_path):
    box = _Box()
    m, comp = _model(tmp_path, [
        _Resp(tool_calls=[_ToolCall("call_1", "get_market", "not-json")]),
        _Resp('{"orders":[]}'),
    ], toolbox=box)
    assert m._chat({}) == {"orders": []}
    assert box.trace[0]["args"] == {}
