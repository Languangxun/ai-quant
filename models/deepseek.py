import os
import json
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

from models.base import BaseModel
from models.prompt import SYSTEM_PROMPT
from models.config import load_model_config


load_dotenv()

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
                 context_dir=None):
        """
        daily_context=True 时启用「一天一个会话」：当日历史多轮持久化在
        context_dir/llm_daily_YYYY-MM-DD.json，每次请求 messages =
        [system] + 当日历史 + 新 user 消息。DeepSeek 按前缀缓存计费
        (命中部分约 1/10 价格)，历史段稳定不变 → 逐时点请求大面积命中；
        跨天自动开新会话。仅追加成功(可解析)的轮次，失败重试的前缀不变。
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

    def _chat(self, context):

        user_msg = json.dumps(
            context,
            ensure_ascii=False
        )

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

        response = self.client.chat.completions.create(

            model=self.model,

            temperature=self.temperature,

            messages=messages

        )

        content = response.choices[0].message.content

        result = extract_json(content)

        if doc is not None:
            # 仅在解析成功后追加: 失败重试时前缀与上次完全一致, 仍吃缓存
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
