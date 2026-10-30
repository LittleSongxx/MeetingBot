# MySQL 8.4 + 初始化 SQL 的数据库镜像
#
# 为什么把 SQL 打进镜像，而不是在 compose 里 bind mount 两个 .sql 文件：
# 部署约定不允许任何宿主 bind mount（Windows/WSL 的路径、权限、CRLF 差异
# 都会让"空卷初始化"这个一次性动作变得不可复现）。SQL 打进镜像后，
# 官方 entrypoint 在数据卷为空时按文件名顺序执行一次：
#   01-schema.sql 建 14 张表 -> 02-seed.sql 插 2 个部门 + prompt 模板。
#
# 注意：初始化只在**空数据卷**时执行。改了 SQL 要重新构建本镜像
# （docker compose build db），并且只在需要重建库时删掉 mysql_data 卷；
# 已有数据卷不会重放初始化脚本。新增表请同时改 01-schema.sql 与
# meetingbot/fastapi-app/models.py。
FROM mysql:8.4.11

COPY 01-schema.sql /docker-entrypoint-initdb.d/01-schema.sql
COPY 02-seed.sql /docker-entrypoint-initdb.d/02-seed.sql
