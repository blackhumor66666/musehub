"""面板配置项测试: 校验 / 持久化 / 热应用 / 重启后仍然生效."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import settings as S
from app.config import Config
from app.runtime import Context


class TestSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="musehub-set-"))
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ["MUSIC_DIR"] = str(self.tmp / "music")
        os.environ["MUSEHUB_MOCK"] = "1"
        os.environ["DRY_RUN"] = "1"
        os.environ["NAVIDROME_URL"] = ""
        os.environ["NETEASE_COOKIE"] = ""
        self.cfg = Config.load(env_file=self.tmp / "none.env")
        self.ctx = Context(self.cfg)

    def tearDown(self) -> None:
        for p in self.ctx.providers.values():
            p.stop()
        self.ctx.db.close()

    def test_schema_shape(self) -> None:
        data = S.schema(self.ctx.cfg, self.ctx.db)
        ids = [g["id"] for g in data["groups"]]
        self.assertEqual(ids, ["platform", "navidrome", "telegram", "notify",
                               "sync", "organizer", "web"])
        keys = {f["key"] for g in data["groups"] for f in g["fields"]}
        # 旧的单账号 Cookie 已合并进「多账号」, 不再下发到面板
        for k in ("netease.cookie", "qq.cookie", "navidrome.url", "telegram.token",
                  "sync.delete_policy", "organizer.album_template",
                  "notify.enabled", "notify.webhook_url", "notify.quiet_hours"):
            self.assertIn(k, keys)

    def test_validate(self) -> None:
        self.assertTrue(S.validate("navidrome.url", "ftp://x"))
        self.assertEqual(S.validate("navidrome.url", "http://192.168.5.13:4533"), "")
        self.assertTrue(S.validate("sync.interval_minutes", "abc"))
        self.assertTrue(S.validate("sync.interval_minutes", "1"))
        self.assertEqual(S.validate("sync.interval_minutes", "60"), "")
        self.assertTrue(S.validate("organizer.album_template", "{albumartist}/{album}"))
        self.assertEqual(S.validate("organizer.album_template", "{albumartist}/{album}/{title}"), "")
        self.assertTrue(S.validate("telegram.admin_ids", "12,x"))
        self.assertEqual(S.validate("telegram.admin_ids", "12,34"), "")

    def test_save_applies_hot(self) -> None:
        changed, errors = S.save(self.ctx.cfg, self.ctx.db, {
            "netease.cookie": "MUSIC_U=deadbeef; __csrf=abc",
            "sync.interval_minutes": "30",
            "navidrome.url": "http://192.168.5.13:4533",
        })
        self.assertEqual(errors, [])
        self.assertEqual(set(changed), {"netease.cookie", "sync.interval_minutes", "navidrome.url"})

        warnings = S.apply_runtime(self.ctx, changed)
        self.assertTrue(isinstance(warnings, list))

        self.assertEqual(self.ctx.cfg.sync.interval_minutes, 30)
        self.assertEqual(self.ctx.navidrome.base, "http://192.168.5.13:4533")
        self.assertEqual(self.ctx.cfg.netease.cookie, "MUSIC_U=deadbeef; __csrf=abc")

    def test_provider_reload_without_restart(self) -> None:
        """热加载: 换 Cookie 后立刻生效, 清空后立刻失效."""
        from app.config import NeteaseConfig, QQConfig
        from app.providers.netease import NeteaseProvider
        from app.providers.qq import QQProvider

        ncfg = NeteaseConfig(cookie="")
        ne = NeteaseProvider(ncfg)
        self.assertFalse(ne.logged_in())

        ncfg.cookie = "MUSIC_U=deadbeef; __csrf=abc"
        ne.reload()
        self.assertTrue(ne.logged_in())
        self.assertEqual(ne.cookies.get("MUSIC_U"), "deadbeef")
        self.assertEqual(ne.csrf, "abc")
        self.assertIn("MUSIC_U=deadbeef", ne.session.headers.get("Cookie", ""))

        ncfg.cookie = ""
        ne.reload()
        self.assertFalse(ne.logged_in())
        self.assertNotIn("Cookie", ne.session.headers)

        qcfg = QQConfig(cookie="", uin="", favorite_dissid="")
        qq = QQProvider(qcfg)
        self.assertFalse(qq.logged_in())
        qcfg.cookie = "uin=o0012345; qm_keyst=xyz"
        qcfg.favorite_dissid = "987654"
        qq.reload()
        self.assertTrue(qq.logged_in())
        self.assertEqual(qq.uin, "12345")
        self.assertEqual(qq.favorite_playlist_id(), "987654")


    def test_accounts_are_merged_into_single_cookie(self) -> None:
        """多账号退役: 列表里的第一个 Cookie 合并进单字段, 列表清空."""
        db = self.ctx.db
        db.kv_set("cfg:qq.accounts", "默认=pgv_pvid=1; qm_keyst=K")
        S.load_into(self.ctx.cfg, db)
        self.assertEqual(db.kv_get("cfg:qq.cookie", ""), "pgv_pvid=1; qm_keyst=K")
        self.assertEqual(db.kv_get("cfg:qq.accounts", ""), "", "合并后应清空多账号")
        self.assertEqual(self.ctx.cfg.qq.cookie, "pgv_pvid=1; qm_keyst=K")
        self.assertEqual(self.ctx.cfg.qq.accounts, [])

    def test_single_cookie_is_used_directly(self) -> None:
        """只填一个 Cookie 就能用(不需要再维护账号列表)."""
        db = self.ctx.db
        db.kv_set("cfg:netease.cookie", "MUSIC_U=legacy")
        S.load_into(self.ctx.cfg, db)
        self.assertEqual(self.ctx.cfg.netease.cookie, "MUSIC_U=legacy")
        self.assertEqual(self.ctx.cfg.netease.accounts, [])

    def test_hidden_fields_not_in_schema(self) -> None:
        data = S.schema(self.ctx.cfg, self.ctx.db)
        keys = [f["key"] for g in data["groups"] for f in g["fields"]]
        self.assertIn("qq.cookie", keys, "现在只保留单 Cookie 入口")
        self.assertNotIn("qq.accounts", keys, "多账号已合并, 不该再出现在面板上")
        self.assertNotIn("qq.download_account", keys, "下载账号已去掉")


    def test_raw_cookie_line_is_not_split(self) -> None:
        """直接粘贴整行 Cookie 时不能按第一个 = 拆开(线上踩过: 账号名变成 pgv_pvid)."""
        raw = "pgv_pvid=2423591868; fqm_pvqid=b6ff; qm_keyst=W_X_abc; qqmusic_key=W_X_abc"
        accs = S.parse_accounts(raw)
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0][0], "默认")
        self.assertEqual(accs[0][1], raw, "整行 Cookie 必须原样保留")

    def test_named_account_still_works(self) -> None:
        raw = "我=MUSIC_U=deadbeef; __csrf=abc\n小号=MUSIC_U=zzz"
        accs = S.parse_accounts(raw)
        self.assertEqual([a[0] for a in accs], ["我", "小号"])
        self.assertEqual(accs[0][1], "MUSIC_U=deadbeef; __csrf=abc")

    def test_legacy_split_account_is_normalized(self) -> None:
        """老数据(账号名=pgv_pvid)合并进单 Cookie 时要还原成完整 Cookie."""
        db = self.ctx.db
        db.kv_set("cfg:qq.accounts", "pgv_pvid=2423591868; qm_keyst=K")
        S.load_into(self.ctx.cfg, db)
        self.assertEqual(db.kv_get("cfg:qq.cookie", ""),
                         "pgv_pvid=2423591868; qm_keyst=K")

    def test_persist_across_restart(self) -> None:
        S.save(self.ctx.cfg, self.ctx.db, {
            "netease.cookie": "MUSIC_U=persist",
            "sync.dry_run": "0",
            "telegram.admin_ids": "11,22",
        })
        self.ctx.db.close()
        for p in self.ctx.providers.values():
            p.stop()

        cfg2 = Config.load(env_file=self.tmp / "none.env")
        self.assertTrue(cfg2.sync.dry_run, "env 默认还是试运行")
        from app.db import Database
        db2 = Database(cfg2.db_path)
        applied = S.load_into(cfg2, db2)
        db2.close()
        self.assertIn("sync.dry_run", applied)
        self.assertFalse(cfg2.sync.dry_run, "面板里存的实盘应覆盖 .env")
        self.assertEqual(cfg2.netease.cookie, "MUSIC_U=persist")
        self.assertEqual(cfg2.telegram.admin_ids, (11, 22))

    def test_dry_run_and_policy_share_legacy_keys(self) -> None:
        S.save(self.ctx.cfg, self.ctx.db, {"sync.dry_run": "0", "sync.delete_policy": "never"})
        self.assertEqual(self.ctx.db.kv_get("dry_run"), "0")
        self.assertEqual(self.ctx.db.kv_get("delete_policy"), "never")
        self.assertFalse(self.ctx.engine.dry_run)
        self.assertEqual(self.ctx.engine.delete_policy, "never")

    def test_unknown_key_ignored(self) -> None:
        changed, errors = S.save(self.ctx.cfg, self.ctx.db, {"nope.key": "1"})
        self.assertEqual(changed, [])
        self.assertEqual(errors, [])

    def test_web_password_can_be_saved(self) -> None:
        """回归: 面板密码曾因 Config 上没有 web 分组而 500."""
        self.assertEqual(self.ctx.cfg.web_password, "")
        changed, errors = S.save(self.ctx.cfg, self.ctx.db, {"web.password": "s3cret"})
        self.assertEqual(errors, [])
        self.assertIn("web.password", changed)
        self.assertEqual(self.ctx.cfg.web_password, "s3cret")
        # 读回来也要对
        self.assertEqual(S.current_values(self.ctx.cfg, self.ctx.db)["web.password"], "s3cret")

    def test_quality_chain_derivation(self) -> None:
        changed, errors = S.save(self.ctx.cfg, self.ctx.db, {"netease.max_quality": "exhigh"})
        self.assertEqual(errors, [])
        self.assertIn("netease.max_quality", changed)
        self.assertEqual(self.ctx.cfg.netease.max_quality, "exhigh")
        self.assertEqual(self.ctx.cfg.netease.quality_chain,
                         ("exhigh", "higher", "standard"))

        changed, errors = S.save(self.ctx.cfg, self.ctx.db, {"qq.max_quality": "m800"})
        self.assertEqual(errors, [])
        self.assertEqual(self.ctx.cfg.qq.quality_chain, ("m800", "c400", "m500"))

        _, errors = S.save(self.ctx.cfg, self.ctx.db, {"qq.max_quality": "ultra"})
        self.assertTrue(errors, "非法音质档位必须被拒绝")

    def test_quality_persist_and_reload(self) -> None:
        S.save(self.ctx.cfg, self.ctx.db, {"netease.max_quality": "hires"})
        self.ctx.db.close()
        for pr in self.ctx.providers.values():
            pr.stop()
        cfg2 = Config.load(env_file=self.tmp / "none.env")
        from app.db import Database
        db2 = Database(cfg2.db_path)
        S.load_into(cfg2, db2)
        db2.close()
        self.assertEqual(cfg2.netease.max_quality, "hires")
        self.assertEqual(cfg2.netease.quality_chain[0], "hires")

    def test_all_fields_are_writable(self) -> None:
        """每个字段都必须能写进 cfg, 否则面板保存会 500."""
        for f in S.FIELDS:
            obj, attr = S._get_section(self.ctx.cfg, f.key)
            self.assertTrue(hasattr(obj, attr), f"{f.key} 在配置对象上不存在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
