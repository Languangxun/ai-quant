"""订单生命周期记录（借鉴 akquant 的订单事件流）。

状态流转：
    REQUESTED → VALIDATED → FILLED
              ↘ REJECTED / NO_CHANGE      （风控拦截或无需成交）
    HOLD                                   （模型明确不动）

模拟盘把每笔订单状态追加到 sim/state/order_journal.jsonl，便于审计与
状态页复盘；回测不落盘（trades 已是逐笔记录）。
"""
import json
import os
from datetime import datetime

STATUS_REQUESTED = "REQUESTED"
STATUS_VALIDATED = "VALIDATED"
STATUS_FILLED = "FILLED"
STATUS_REJECTED = "REJECTED"
STATUS_NO_CHANGE = "NO_CHANGE"
STATUS_HOLD = "HOLD"


def make_record(code="", action="HOLD", status=STATUS_REQUESTED, **fields):
    rec = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "code": str(code or "").lower(),
        "action": str(action or "HOLD").upper(),
        "status": status,
    }
    rec.update(fields)
    return rec


class OrderJournal:
    """订单状态 JSONL 日志；超过 max_records 时保留最近一半。"""

    def __init__(self, path=None, max_records=20000):
        self.path = path
        self.max_records = int(max_records)
        self.records = []

    def add(self, record=None, **fields):
        rec = record or make_record(**fields)
        self.records.append(rec)
        if self.path:
            try:
                self._append(rec)
            except OSError:
                pass
        return rec

    def _append(self, rec):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if self.max_records and self._count_lines() > self.max_records:
            self._trim()

    def _count_lines(self):
        try:
            with open(self.path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _trim(self):
        keep = max(1, self.max_records // 2)
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()[-keep:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, self.path)

    def recent(self, limit=100):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    items = [json.loads(x) for x in f if x.strip()]
                return items[-limit:]
            except (OSError, ValueError):
                pass
        return self.records[-limit:]
