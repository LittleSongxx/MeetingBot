"""URL 归一化。

历史上的头像地址是上传接口返回的绝对地址（http://127.0.0.1:9090/files/download/x.png），
换机器/换端口就会裂图。现在上传接口返回相对路径，读取侧对老数据做一次归一化：
凡是 host 之后是 /files/... 的绝对地址，统一退化成相对路径，由前端决定拼哪个 origin。
"""

from urllib.parse import urlparse

# 本项目由后端自己托管的静态文件前缀；只有这些才需要归一化
OWNED_PATH_PREFIX = "/files/"


def to_relative_url(value: str | None) -> str | None:
    """绝对地址 → 相对路径；已经是相对路径或不属于本服务的地址原样返回。"""
    if not value:
        return value
    if value.startswith("/"):
        return value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.path.startswith(OWNED_PATH_PREFIX):
        return parsed.path
    return value
