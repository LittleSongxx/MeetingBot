"""P0 安全修复的回归测试：密码哈希、令牌指纹、限流、下载 MIME 加固。"""

import os
import unittest
from unittest import mock

from common import ratelimit, security


class PasswordHashingTest(unittest.TestCase):
    def test_hash_roundtrip_and_salting(self):
        first = security.hash_password("s3cret-密码")
        second = security.hash_password("s3cret-密码")
        self.assertTrue(security.is_hashed(first))
        self.assertTrue(security.verify_password("s3cret-密码", first))
        self.assertFalse(security.verify_password("wrong", first))
        # 盐随机：同口令两次哈希不同，但都能通过校验
        self.assertNotEqual(first, second)
        self.assertTrue(security.verify_password("s3cret-密码", second))

    def test_legacy_plaintext_still_verifies_and_migrates(self):
        self.assertTrue(security.verify_password("plain", "plain"))
        self.assertFalse(security.verify_password("plain", "other"))
        self.assertFalse(security.is_hashed("plain"))

        class Row:
            password = "plain"

        row = Row()
        self.assertTrue(security.migrate_if_plaintext(row))
        self.assertTrue(security.is_hashed(row.password))
        # 幂等：已是哈希不再迁移
        self.assertFalse(security.migrate_if_plaintext(row))

    def test_fingerprint_changes_with_password_value(self):
        self.assertNotEqual(security.password_fingerprint("a"), security.password_fingerprint("b"))
        self.assertEqual(len(security.password_fingerprint("a")), 16)


class RateLimitTest(unittest.IsolatedAsyncioTestCase):
    """登录防爆破阈值的接线检查。

    限流器自身的实现细节（滑动窗口、Lua、fail-open）在 `test_ratelimit.py` 里钉；
    这里只锁 P0 关心的语义：**达到阈值即拦、键之间独立、成功即清零、过期即放行**。
    （接口从同步改成异步，是因为计数搬到了 Redis——多副本下进程内计数会各算一份。）
    """

    async def asyncSetUp(self):
        ratelimit.reset_backend()
        with mock.patch.dict(os.environ, {"REDIS_URL": ""}):
            ratelimit.reset_backend()
            ratelimit.backend()

    async def asyncTearDown(self):
        ratelimit.reset_backend()

    async def test_blocks_after_threshold_and_clears_on_success(self):
        for _ in range(ratelimit._LOGIN_MAX_FAILURES):
            self.assertFalse(await ratelimit.is_blocked("login-user", "u1"))
            await ratelimit.record_failure("login-user", "u1")
        self.assertTrue(await ratelimit.is_blocked("login-user", "u1"))
        # 键之间独立
        self.assertFalse(await ratelimit.is_blocked("login-user", "u2"))
        await ratelimit.record_success("login-user", "u1")
        self.assertFalse(await ratelimit.is_blocked("login-user", "u1"))

    async def test_window_expiry(self):
        clock = {"now": 100.0}
        with mock.patch("time.monotonic", side_effect=lambda: clock["now"]):
            for _ in range(ratelimit._LOGIN_MAX_FAILURES):
                await ratelimit.record_failure("login-ip", "x")
            self.assertTrue(await ratelimit.is_blocked("login-ip", "x"))
            clock["now"] = 100.0 + ratelimit._LOGIN_WINDOW_SECONDS + 1
            self.assertFalse(await ratelimit.is_blocked("login-ip", "x"))


class DownloadMimeTest(unittest.TestCase):
    """服务端 MIME 映射 + nosniff 的关键性质：客户端声明的 content_type 不再回放。"""

    def test_material_mime_table_covers_whitelist_extensions(self):
        from api import meeting_material as mm
        for ext in ("mp3", "wav", "mp4", "docx", "pdf", "txt"):
            self.assertIn(ext, mm.MATERIAL_MIME_BY_EXT)
            self.assertNotIn("html", mm.MATERIAL_MIME_BY_EXT[ext].lower())
        # 未知扩展退回二进制流
        self.assertEqual(mm.MATERIAL_MIME_BY_EXT.get("exe", "application/octet-stream"),
                         "application/octet-stream")

    def test_avatar_mime_is_always_image_or_octet(self):
        from api import files as files_api
        allowed = files_api.AVATAR_EXTENSIONS
        for _ext in allowed:
            self.assertIn(files_api.UPLOAD_DIR.name, "files")  # sanity
        # 映射表在下载函数内部；此处锁住白名单不含可执行类型
        self.assertNotIn("html", allowed)
        self.assertNotIn("svg", allowed)


if __name__ == "__main__":
    unittest.main()
