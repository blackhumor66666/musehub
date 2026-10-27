"""Telegram API 封装的行为测试(不联网).

重点是防回归: getUpdates 的 API 参数就叫 timeout(长轮询秒数),
而 HTTP 超时参数必须另起名字, 否则会 TypeError 导致轮询整个挂掉.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tg.api import TgApi, TgError  # noqa: E402


class _Resp:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status_code = status
        self.text = ""

    def json(self) -> dict:
        return self._data


class TestTgApi(unittest.TestCase):
    def setUp(self) -> None:
        self.api = TgApi("123456789:AAFfakefakefakefakefakefakefake")
        self.captured: dict = {}

    def _patch(self, data: dict) -> None:
        def fake_post(url, json=None, timeout=None, proxies=None):  # noqa: A002
            self.captured.update({"url": url, "payload": json, "http_timeout": timeout})
            return _Resp(data)

        self.api.session.post = fake_post  # type: ignore[assignment]

    def test_get_updates_keeps_timeout_as_api_param(self) -> None:
        """timeout 要原样进 JSON body, HTTP 超时要更长(长轮询)."""
        self._patch({"ok": True, "result": []})
        out = self.api.get_updates(0, timeout=5)
        self.assertEqual(out, [])
        self.assertEqual(self.captured["payload"]["timeout"], 5)
        self.assertEqual(self.captured["payload"]["offset"], 0)
        self.assertEqual(self.captured["http_timeout"], 20)
        self.assertIn("/getUpdates", self.captured["url"])

    def test_get_updates_returns_list(self) -> None:
        self._patch({"ok": True, "result": [{"update_id": 1}]})
        self.assertEqual(len(self.api.get_updates(1)), 1)

    def test_call_reports_telegram_error(self) -> None:
        self._patch({"ok": False, "error_code": 404, "description": "Not Found"})
        with self.assertRaises(TgError):
            self.api.me()

    def test_send_message_payload(self) -> None:
        self._patch({"ok": True, "result": {"message_id": 1}})
        self.api.send(123, "hi", markup={"inline_keyboard": []})
        payload = self.captured["payload"]
        self.assertEqual(payload["chat_id"], 123)
        self.assertEqual(payload["text"], "hi")
        self.assertIn("reply_markup", payload)
        self.assertTrue(payload["parse_mode"])


if __name__ == "__main__":
    unittest.main()
