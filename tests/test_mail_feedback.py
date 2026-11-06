"""用户邮件反馈读取的单元测试（不连接 IMAP）。"""
import json
import os
import sys
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import mail_feedback as mf


def _raw(subject="反馈", body="你太保守了", from_="user@example.com",
         reply=False):
    raw = (
        f"From: {from_}\n"
        "To: bot@example.com\n"
        f"Subject: {Header(('Re: ' if reply else '') + subject, 'utf-8').encode()}\n"
        "Date: Wed, 04 Nov 2026 20:00:00 +0800\n"
        "Message-ID: <abc@example.com>\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        f"{body}\n"
    )
    return raw.encode("utf-8")


def _setup_store(monkeypatch, tmp_path):
    monkeypatch.setattr(mf, "STORE_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr(mf, "MAIL_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_AUTH_CODE", "test-code")
    monkeypatch.setenv("MAIL_RECIPIENTS", "user@example.com")


def test_parse_and_strip_quote():
    raw = _raw(body="你太保守了，多买点\n\n-----原始邮件-----\n> 原报告\n")
    item = mf._parse_message(raw, "1")
    assert item["subject"] == "反馈"
    assert item["text"].startswith("你太保守了，多买点")
    assert "原报告" not in item["text"]


def test_multipart_prefers_plain():
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("纯文本内容", "plain", "utf-8"))
    msg.attach(MIMEText("<b>HTML内容</b>", "html", "utf-8"))
    assert "纯文本内容" in mf._message_text(msg)


def test_allowed_filters_self_and_auto_subjects():
    allowed = {"user@example.com"}
    item = {
        "from": "User <user@example.com>",
        "subject": "Re: ai-quant 模拟盘复盘 2026-11-04",
        "text": "骂一句",
    }
    assert mf._is_allowed(item, allowed)
    assert not mf._is_allowed(
        dict(item, **{"from": "other@example.com"}), allowed)
    assert not mf._is_allowed(
        dict(item, subject="ai-quant 模拟盘复盘 2026-11-04"), allowed)


def test_fetch_store_dedupe_and_min_interval(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    msg = mf._parse_message(_raw(body="别买债券ETF"), "2")
    calls = {"n": 0}

    def fake_fetch(conf, days=7, max_messages=50):
        calls["n"] += 1
        return [dict(msg, fetched_at=time.time())]

    monkeypatch.setattr(mf, "_fetch_messages", fake_fetch)
    assert mf.fetch_and_store(force=True) == 1
    assert mf.fetch_and_store(force=True) == 0
    assert calls["n"] == 2
    # 未到 min_interval 不再请求
    monkeypatch.setattr(mf, "_fetch_messages",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("不应请求")))
    assert mf.fetch_and_store(min_interval=9999) == 0
    items = mf.load_recent()
    assert items and "别买债券ETF" in items[0]["text"]


def test_fetch_failure_keeps_cache(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    msg = mf._parse_message(_raw(body="仓位太重"), "3")
    monkeypatch.setattr(mf, "_fetch_messages",
                        lambda *a, **k: [dict(msg, fetched_at=time.time())])
    mf.fetch_and_store(force=True)

    def boom(*a, **k):
        raise RuntimeError("imap down")

    monkeypatch.setattr(mf, "_fetch_messages", boom)
    out = mf.load_for_decision(fetch=True)
    assert out and "仓位太重" in out["items"][0]["text"]
    assert "note" in out


def test_load_for_decision_empty(monkeypatch, tmp_path):
    _setup_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mf, "_fetch_messages", lambda *a, **k: [])
    assert mf.load_for_decision() is None


def test_fetch_messages_imap_flow(monkeypatch):
    raw = _raw(subject="Re: ai-quant 模拟盘复盘 2026-11-04",
               body="别买债券ETF了", reply=True)
    header = b"From: user@example.com\nSubject: Re: x\n"

    class FakeIMAP:
        def __init__(self, *a, **kw):
            self.fetches = []

        def login(self, *a):
            return "OK", [b""]

        def xatom(self, *a):
            return "OK", [b""]

        def select(self, *a, **kw):
            return "OK", [b"1"]

        def search(self, *a):
            return "OK", [b"1 2"]

        def fetch(self, uid, spec):
            self.fetches.append(spec)
            return "OK", [(b"1 (BODY[] {1}", raw if "HEADER" not in spec
                          else header)]

        def logout(self):
            pass

    fake = FakeIMAP()
    monkeypatch.setattr(mf.imaplib, "IMAP4_SSL",
                        lambda *a, **kw: fake)
    out = mf._fetch_messages({
        "MAIL_USER": "bot@example.com", "MAIL_AUTH_CODE": "x",
        "MAIL_RECIPIENTS": "user@example.com"})
    assert out and "别买债券ETF了" in out[0]["text"]
    assert any("HEADER.FIELDS" in s for s in fake.fetches)
    assert any("BODY.PEEK[]" in s for s in fake.fetches)
