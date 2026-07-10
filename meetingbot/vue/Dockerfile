# 构建阶段：用 node:22 安装依赖并打包静态文件
FROM node:22-alpine AS build

WORKDIR /app

# 先只拷贝依赖清单，依赖没变时可以复用 npm ci 这一层的缓存
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再拷贝其余源码，src、public、index.html 等
COPY . .

# 构建时注入前端变量：VITE_BASE_URL 指向 nginx 的 /api 前缀，
# VITE_REGISTER_ENABLED 关闭生产环境的注册入口
ARG VITE_BASE_URL=/api
ARG VITE_REGISTER_ENABLED=false
RUN VITE_BASE_URL="$VITE_BASE_URL" VITE_REGISTER_ENABLED="$VITE_REGISTER_ENABLED" npm run build

# 运行阶段：只需要 nginx 和打包产物
FROM nginx:1.30.5-alpine3.24

# 用项目自己的 nginx 配置覆盖默认站点，负责 /api/ 与 /files/ 的反代
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# 健康检查走 nginx 的 /health，返回 ok 才算容器就绪
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1
