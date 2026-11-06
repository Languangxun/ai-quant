import os
import json
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

from models.base import BaseModel
from models.prompt import SYSTEM_PROMPT
from models.config import load_model_config
from models.web_search import SEARCH_NOTE, build_from_config


load_dotenv()


def _normalize_socks_proxy():
    """httpx 只认 socks5://且需要 socksio；部分桌面环境写成 socks://。

    不处理会在 OpenAI 客户端初始化时直接 ValueError/ImportError，导致
    LLM 永久降级为纯代码。这里只在进程内兼容：
    - 装了 socksio：socks:// -> socks5://
    - 没装：删除无法使用的 ALL_PROXY，退回 HTTP(S)_PROXY 或直连
    不改用户配置文件。
    """
    try:
        import socksio  # noqa: F401
        socks_ok = True
    except ImportError:
        socks_ok = False
    for key in ("ALL_PROXY", "all_proxy"):
        value = os.getenv(key)
        if not (value and value.lower().startswith("socks://")):
            continue
        if socks_ok:
            os.environ[key] = "socks5://" + value[len("socks://"):]
        else:
            # 没有 socksio 时 httpx 完全无法使用该代理，留着只会让
            # OpenAI 初始化失败；删除后退回 HTTP(S)_PROXY 或直连。
            os.environ.pop(key, None)


_normalize_socks_proxy()

REQUEST_TIMEOUT = 60
API_MAX_RETRIES = 2
PARSE_MAX_RETRIES = 2

QUOTA_STATUS = {401, 402, 403, 429}
QUOTA_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "credit",
    "余额不足",
    "欠费",
    "error code: 401",
    "error code: 402",
    "error code: 403",
    "error code: 429",
)


def is_quota_error(exc):
    for attr in ("status_code", "code"):
        try:
            value = getattr(exc, attr, None)
            if value in QUOTA_STATUS:
                return True
        except TypeError:
            pass
    text = str(exc or "").lower()
    return any(marker in text for marker in QUOTA_MARKERS)


NETWORK_MARKERS = (
    "connection error",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "max retries exceeded",
    "timed out",
    "timeout",
    "ssl",
    "eof occurred",
    "remotedisconnected",
)


def is_network_error(exc):
    text = str(exc or "").lower()
    return any(marker in text for marker in NETWORK_MARKERS)


def extract_json(text):
    """从模型输出中稳健地提取 JSON（容忍 markdown 围栏和多余文字）。"""
    if text is None:
        raise ValueError("空响应")
    text = text.strip()
    # 去掉 ```json ... ``` 围栏
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"无法解析 JSON: {text[:120]}")


class DeepSeekModel(BaseModel):

    def __init__(self, system_prompt=None, daily_context=False,
                 context_dir=None, search_client=None):
        """
        daily_context=True 时启用「一天一个会话」：当日历史多轮持久化在
        context_dir/llm_daily_YYYY-MM-DD.json，每次请求 messages =
        [system] + 当日历史 + 新 user 消息。DeepSeek 按前缀缓存计费
        (命中部分约 1/10 价格)，历史段稳定不变 → 逐时点请求大面积命中；
        跨天自动开新会话。仅追加成功(可解析)的轮次，失败重试的前缀不变。

        search_client：决策前自动联网检索（DeepSeek Search API）。
        None=按 config/model.yaml 自动启用；False=显式关闭；也可注入客户端。
        """
        config = load_model_config()

        decision_config = config["models"]["decision"]

        self.model = decision_config["model"]

        self.base_url = (
            decision_config.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        )

        self.temperature = 0.2

        self.system_prompt = system_prompt or SYSTEM_PROMPT

        # 工具调用（借鉴桌面 GUI 的 AI tool calling）：
        # toolbox 由调用方按决策上下文注入；None=不启用。接口不支持 tools
        # 时自动降级为纯 JSON，绝不打断交易决策。
        tools_cfg = decision_config.get("tools") or {}
        self.tools_enabled = bool(tools_cfg.get("enabled", True))
        self.tool_max_rounds = max(0, int(tools_cfg.get("max_rounds", 3)))
        self.toolbox = None
        self.tools_available = True
        self.last_tool_trace = []

        self.daily_context = daily_context
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.context_dir = context_dir or os.path.join(_base, "sim", "state")

        api_key = os.getenv(
            "DEEPSEEK_API_KEY"
        )

        if not api_key:

            raise ValueError(
                "DEEPSEEK_API_KEY 未配置"
            )

        self.client = OpenAI(

            api_key=api_key,

            base_url=self.base_url,

            timeout=REQUEST_TIMEOUT,

            max_retries=API_MAX_RETRIES,

        )

        # 自动联网检索：DeepSeek Search API（失败静默降级，不影响决策）
        if search_client is False:
            self.search_client = None
        elif search_client is not None:
            self.search_client = search_client
        else:
            self.search_client = build_from_config(
                config, api_key=api_key)

    def _augment(self, context):
        """决策前自动检索并把摘要并入上下文；任何异常都保持原样。"""
        if not self.search_client or not isinstance(context, dict):
            return context, 0
        try:
            results = self.search_client.gather(context)
        except Exception as e:
            print(f"[search] 自动检索失败（忽略）：{e}", flush=True)
            return context, 0
        if not results:
            return context, 0
        enriched = dict(context)
        enriched["web_search"] = {
            "note": SEARCH_NOTE,
            "results": results,
        }
        ev = getattr(self.search_client, "last_event", None)
        if isinstance(ev, dict) and ev.get("items"):
            enriched["event_watch"] = {
                "note": SEARCH_NOTE,
                "date": ev.get("date"),
                "days": ev.get("days"),
                "topics": ev.get("topics"),
                "salience": ev.get("salience"),
                "signals": ev.get("signals"),
                "brief": ev.get("brief"),
                "items": ev.get("items")[:6],
            }
        return enriched, len(results)

    # ---------- 日内会话 (前缀缓存优化) ----------

    def _ctx_path(self, day):
        return os.path.join(self.context_dir, f"llm_daily_{day}.json")

    def _load_history(self):
        """读当日会话; 跨天/无文件/损坏都返回全新文档 (绝不动旧前缀)."""
        day = time.strftime("%Y-%m-%d")
        try:
            with open(self._ctx_path(day), encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("date") == day and isinstance(doc.get("turns"), list):
                doc.setdefault("usage", [])
                return doc
        except Exception:
            pass
        return {"date": day, "turns": [], "usage": []}

    def _save_history(self, doc):
        os.makedirs(self.context_dir, exist_ok=True)
        p = self._ctx_path(doc["date"])
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
        self._gc_history()

    def _gc_history(self, keep_days=7):
        """顺手清理过期会话文件 (sim/state 不入 git)."""
        try:
            import glob
            cutoff = time.strftime(
                "%Y-%m-%d", time.gmtime(time.time() - keep_days * 86400))
            for p in glob.glob(os.path.join(
                    self.context_dir, "llm_daily_*.json")):
                d = os.path.basename(p)[len("llm_daily_"):-len(".json")]
                if d < cutoff:
                    os.remove(p)
        except Exception:
            pass

    @staticmethod
    def _tools_unsupported(exc):
        """判断异常是否因接口/模型不支持 tools（而非额度、网络）。"""
        status = getattr(exc, "status_code", None)
        text = str(exc or "").lower()
        if status not in (400, 404, 422) and "tool" not in text:
            return False
        return ("tool" in text or "function calling" in text
                or "function_call" in text)

    def _create(self, messages, use_tools):
        """一次 API 请求；tools 不被支持时自动去掉并记住该状态。"""
        kwargs = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if (use_tools and self.toolbox is not None
                and self.tools_available and self.tools_enabled):
            kwargs["tools"] = self.toolbox.specs()
            kwargs["tool_choice"] = "auto"
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as e:
            if "tools" in kwargs and self._tools_unsupported(e):
                self.tools_available = False
                print("[llm] 接口不支持工具调用，降级为纯 JSON 决策",
                      flush=True)
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                return self.client.chat.completions.create(**kwargs)
            raise

    @staticmethod
    def _assistant_tool_msg(message):
        """OpenAI 消息对象 -> 可回传的 assistant(tool_calls) 字典。"""
        calls = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            calls.append({
                "id": getattr(tc, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", "") or "",
                    "arguments": getattr(fn, "arguments", "") or "{}",
                },
            })
        return {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": calls,
        }

    def _chat(self, context):

        context, search_count = self._augment(context)

        user_msg = json.dumps(
            context,
            ensure_ascii=False
        )

        if search_count:
            print(f"[search] 自动检索 {search_count} 条，已并入决策上下文",
                  flush=True)

        doc = self._load_history() if self.daily_context else None

        messages = [

            {
                "role": "system",
                "content": self.system_prompt
            }

        ]
        for t in (doc["turns"] if doc else []):
            messages.append({"role": "user", "content": t["user"]})
            messages.append({"role": "assistant", "content": t["assistant"]})

        messages.append({

            "role": "user",

            "content": user_msg

        })

        # 工具结果依赖联网检索；调用方未注入 search_client 时补上
        if self.toolbox is not None:
            if getattr(self.toolbox, "search_client", None) is None:
                self.toolbox.search_client = self.search_client
            self.last_tool_trace = []

        response = None
        content = ""
        rounds = 0
        use_tools = (self.toolbox is not None and self.tools_enabled
                     and self.tool_max_rounds > 0)
        while True:
            response = self._create(messages, use_tools)
            message = response.choices[0].message
            calls = list(getattr(message, "tool_calls", None) or [])
            if not calls:
                content = message.content or ""
                break
            if not use_tools or not self.tools_available:
                # 接口异常返回了工具调用但无法继续：直接使用文本
                content = message.content or ""
                break
            messages.append(self._assistant_tool_msg(message))
            for tc in calls:
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", "") or ""
                raw_args = getattr(fn, "arguments", "") or "{}"
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except (TypeError, ValueError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = self.toolbox.call(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "") or "",
                    "content": str(result),
                })
                print(f"[tool] {name}({json.dumps(args, ensure_ascii=False)[:80]})"
                      f" -> {str(result)[:120]}", flush=True)
            rounds += 1
            if rounds >= self.tool_max_rounds:
                # 到上限后强制收口，避免无限取数（GET 类工具无副作用）
                messages.append({
                    "role": "user",
                    "content": "工具调用已达上限，请基于已获取的信息直接输出最终 JSON。",
                })
                use_tools = False

        if self.toolbox is not None:
            self.last_tool_trace = list(getattr(self.toolbox, "trace", []) or [])

        result = extract_json(content)

        if doc is not None:
            # 仅在解析成功后追加: 失败重试时前缀与上次完全一致, 仍吃缓存。
            # 只存最终 user/assistant（不含中间 tool 消息），保持跨时点前缀稳定。
            usage = getattr(response, "usage", None)
            hit = getattr(usage, "prompt_cache_hit_tokens", None)
            miss = getattr(usage, "prompt_cache_miss_tokens", None)
            doc["turns"].append({
                "time": time.strftime("%H:%M"),
                "user": user_msg, "assistant": content,
            })
            doc["usage"].append({
                "time": time.strftime("%H:%M"),
                "cache_hit": hit, "cache_miss": miss,
                "completion": getattr(usage, "completion_tokens", None),
            })
            self._save_history(doc)
            print(f"[llm] 当日第{len(doc['turns'])}轮 前缀缓存命中={hit} "
                  f"未命中={miss} tokens", flush=True)

        return result



    def analyze(self, context):

        """带重试的分析：网络异常或 JSON 解析失败时重试。"""

        last_err = None

        for attempt in range(PARSE_MAX_RETRIES + 1):

            try:

                return self._chat(context)

            except Exception as e:

                last_err = e

                if attempt < PARSE_MAX_RETRIES:

                    time.sleep(2 * (attempt + 1))

        raise RuntimeError(

            f"DeepSeek 决策失败（已重试 {PARSE_MAX_RETRIES} 次）: {last_err}"

        )
