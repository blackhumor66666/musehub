"""下载账号选择: 下载固定走"会员账号", 与歌单由谁订阅无关.

背景: 多账号订阅时, 如果下载用了没会员的小号, 会整批失败。
所以下载路径按 `download_account` 选 provider, 留空回落到第一个。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloader import Downloader


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.cfg = SimpleNamespace(quality_chain=("m800",))


class TestDownloadProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = {
            "qq": _FakeProvider("qq:默认"),
            "qq:默认": _FakeProvider("qq:默认"),
            "qq:小号": _FakeProvider("qq:小号"),
            "qq:会员": _FakeProvider("qq:会员"),
        }

    def _dl(self, download_account: str, concurrency: int = 1) -> Downloader:
        cfg = SimpleNamespace(
            qq=SimpleNamespace(download_account=download_account),
            sync=SimpleNamespace(download_concurrency=concurrency),
        )
        return Downloader(cfg, None, self.providers)

    def test_default_is_first_account(self) -> None:
        dl = self._dl("")
        self.assertIs(dl._download_provider("qq"), self.providers["qq"])

    def test_uses_named_account(self) -> None:
        dl = self._dl("会员")
        self.assertIs(dl._download_provider("qq"), self.providers["qq:会员"],
                      "填了下载账号就必须用它")

    def test_falls_back_when_account_missing(self) -> None:
        """账号被删了也不能让下载直接崩, 回落到第一个账号并继续."""
        dl = self._dl("已删除的账号")
        self.assertIs(dl._download_provider("qq"), self.providers["qq"])

    def test_unknown_platform(self) -> None:
        dl = self._dl("会员")
        self.assertIsNone(dl._download_provider("kugou"))


if __name__ == "__main__":
    unittest.main()
