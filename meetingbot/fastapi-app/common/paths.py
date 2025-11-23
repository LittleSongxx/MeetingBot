"""应用使用的磁盘路径。

单独成模块，避免 service 层为了一个目录常量反向 import api 层：
`api/__init__` 会导入所有路由，service → api 很容易形成循环。
"""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FILES_DIR = APP_DIR / "files"
MATERIAL_DIR = FILES_DIR / "meeting_material"

__all__ = ["APP_DIR", "FILES_DIR", "MATERIAL_DIR"]
