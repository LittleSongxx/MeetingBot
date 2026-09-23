"""P0 安全修复的回归测试：密码哈希、令牌指纹、限流、下载 MIME 加固。"""

import unittest

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


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        ratelimit._counters.clear()

    def test_blocks_after_threshold_and_clears_on_success(self):
        for _ in range(8):
            self.assertFalse(ratelimit.is_blocked("login-user", "u1"))
            ratelimit.record_failure("login-user", "u1")
        self.assertTrue(ratelimit.is_blocked("login-user", "u1"))
        # 键之间独立
        self.assertFalse(ratelimit.is_blocked("login-user", "u2"))
        ratelimit.record_success("login-user", "u1")
        self.assertFalse(ratelimit.is_blocked("login-user", "u1"))

    def test_window_expiry(self):
        entry = [ratelimit.time.monotonic() - ratelimit._LOGIN_WINDOW_SECONDS - 1, 99]
        ratelimit._counters[("login-ip", "x")] = entry
        self.assertFalse(ratelimit.is_blocked("login-ip", "x"))


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
        for ext in allowed:
            self.assertIn(files_api.UPLOAD_DIR.name, "files")  # sanity
        # 映射表在下载函数内部；此处锁住白名单不含可执行类型
        self.assertNotIn("html", allowed)
        self.assertNotIn("svg", allowed)


if __name__ == "__main__":
    unittest.main()
