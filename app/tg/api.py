"""Telegram Bot API 的最小封装.

刻意不用 pyTelegramBotAPI / python-telegram-bot:
  * 只用到 sendMessage / editMessageText / answerCallbackQuery / getUpdates
  * 自己控制超时和重试, 不会因为框架内部队列卡住而"整个机器人没反应"
"""
from __future__ import annotations

import json
from typing import Any

import requests

MAX_TEXT = 3800


class TgError(Exception):
    pass


def strip_html(text: str) -> str:
    """去掉 Telegram HTML 标记, 用于"解析失败时降级为纯文本重发"."""
    import html as _html
    import re as _re
    out = _re.sub(r"</?(?:b|strong|i|em|u|s|code|pre|a|tg-spoiler)\b[^>]*>", "", text or "")
    return _html.unescape(out)


def esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TgApi:
    def __init__(self, token: str, proxy: str = ""):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def call(self, method: str, http_timeout: float = 30, **params: Any) -> dict:
        # 注意: HTTP 超时参数叫 http_timeout, 不能叫 timeout ——
        # getUpdates 的 API 参数本身就叫 timeout(长轮询秒数), 撞名会 TypeError
        try:
            resp = self.session.post(
                f"{self.base}/{method}", json=params, timeout=http_timeout, proxies=self.proxies
            )
        except requests.RequestException as exc:
            raise TgError(f"网络错误: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise TgError(f"非 JSON 响应 HTTP {resp.status_code}") from exc
        if not data.get("ok"):
            raise TgError(f"{method} 失败: {data.get('description')} ({data.get('error_code')})")
        return data.get("result") or {}

    # ---------- 常用 ----------
    def send(self, chat_id: int, text: str, markup: dict | None = None,
             reply_to: int | None = None, html: bool = True) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id, "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if html:
            params["parse_mode"] = "HTML"
        if markup:
            params["reply_markup"] = markup
        if reply_to:
            params["reply_to_message_id"] = reply_to
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, markup: dict | None = None) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text[:4096],
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if markup:
            params["reply_markup"] = markup
        return self.call("editMessageText", **params)

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id,
                      text=text[:200], show_alert=alert)
        except TgError:
            pass

    def send_photo(self, chat_id: int, photo: str, caption: str = "") -> dict:
        """发图+说明文字. photo 传公开 URL(Telegram 服务器自己去拉).

        caption 上限 1024 字符, 超了会被服务端拒绝, 这里先截断.
        """
        return self.call("sendPhoto", http_timeout=40, chat_id=chat_id,
                         photo=photo, caption=caption[:1024], parse_mode="HTML")

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        params: dict[str, Any] = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        try:
            result = self.call("getUpdates", http_timeout=timeout + 15, **params)
        except TgError:
            raise
        return result if isinstance(result, list) else []

    def me(self) -> dict:
        return self.call("getMe", http_timeout=15)


def chunks(text: str, size: int = MAX_TEXT) -> list[str]:
    out = []
    while text:
        out.append(text[:size])
        text = text[size:]
    return out or [""]


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows = [[(label, callback_data), ...], ...]"""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
