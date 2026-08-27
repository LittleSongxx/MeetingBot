import unittest

from tortoise import Tortoise

from api import Account, login, register, update_password
from common.auth import create_token, get_current_user
from common.exception_handler import CustomException
from common.security import hash_password, is_hashed, migrate_if_plaintext, password_fingerprint, verify_password
from models import User


class SecurityAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.admin = await User.create(
            username="sec_admin", password=hash_password("admin-pass"), name="管理员", role="ADMIN"
        )

    async def asyncTearDown(self):
        await Tortoise.close_connections()

    def test_hash_verify_and_migrate(self):
        stored = hash_password("p@ssw0rd")
        self.assertTrue(stored.startswith("$2"))
        self.assertTrue(is_hashed(stored))
        self.assertTrue(verify_password("p@ssw0rd", stored))
        self.assertFalse(verify_password("wrong", stored))
        # 遗留明文：识别为未哈希，migrate 后变成哈希且仍可通过校验
        user = User(username="legacy", password="123456", name="旧账号")
        self.assertFalse(is_hashed(user.password))
        self.assertTrue(migrate_if_plaintext(user))
        self.assertTrue(is_hashed(user.password))
        self.assertTrue(verify_password("123456", user.password))
        self.assertFalse(migrate_if_plaintext(user))

    def test_password_fingerprint_changes_with_password(self):
        first = password_fingerprint(hash_password("same"))
        second = password_fingerprint(hash_password("same"))
        # 盐不同 → 同一明文的两次哈希指纹不同；指纹只在库中当前值上计算
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 16)

    async def test_login_upgrades_plaintext_and_returns_token(self):
        await User.create(username="legacy_login", password="123456", name="旧账号", status="NORMAL")
        result = await login(Account(username="legacy_login", password="123456"))
        self.assertEqual(result.code, "200")
        self.assertIn("token", result.data)
        user = await User.get(username="legacy_login")
        # 登录成功后明文被原位升级为 bcrypt
        self.assertTrue(is_hashed(user.password))
        self.assertTrue(verify_password("123456", user.password))

    async def test_login_rejects_wrong_password_and_disabled_account(self):
        with self.assertRaises(CustomException):
            await login(Account(username="sec_admin", password="wrong"))
        await User.create(username="disabled", password=hash_password("123456"), name="停用", status="DISABLED")
        with self.assertRaises(CustomException) as context:
            await login(Account(username="disabled", password="123456"))
        self.assertIn("停用", context.exception.message)

    async def test_change_password_invalidates_old_token(self):
        user = await User.get(id=self.admin.id)
        token = create_token(user)
        # 旧 token 可用
        self.assertEqual((await get_current_user(token)).id, user.id)
        await update_password(
            Account(password="admin-pass", newPassword="admin-pass-2"), current_user=user
        )
        with self.assertRaises(CustomException) as context:
            await get_current_user(token)
        self.assertEqual(context.exception.code, "401")
        # 新密码可登录，旧密码不行
        self.assertEqual((await login(Account(username="sec_admin", password="admin-pass-2"))).code, "200")
        with self.assertRaises(CustomException):
            await login(Account(username="sec_admin", password="admin-pass"))

    async def test_register_respects_switch_and_hashes(self):
        import api

        original = api.REGISTER_ENABLED
        try:
            api.REGISTER_ENABLED = False
            with self.assertRaises(CustomException) as context:
                await register(Account(username="newbie", password="p1", confirmPassword="p1"))
            self.assertEqual(context.exception.code, "403")
            api.REGISTER_ENABLED = True
            result = await register(Account(username="newbie", password="p1", confirmPassword="p1"))
            self.assertEqual(result.code, "200")
            user = await User.get(username="newbie")
            self.assertTrue(is_hashed(user.password))
            self.assertEqual(user.role, "EMPLOYEE")
        finally:
            api.REGISTER_ENABLED = original

    async def test_register_validates_confirm_password(self):
        with self.assertRaises(CustomException):
            await register(Account(username="dup", password="p1", confirmPassword="p2"))


if __name__ == "__main__":
    unittest.main()
