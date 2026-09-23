
# 单测环境的 JWT 密钥：settings 对缺失密钥 fail-fast，测试进程在这里兜底注入。
import os as _os
_os.environ.setdefault("JWT_SECRET", "unit-test-secret-not-for-production")
