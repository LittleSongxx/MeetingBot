import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 测试配置与 vite.config.ts 分开：构建配置里挂着一堆按需引入插件，
// 测试只需要的两件事是"SFC 能编译"和"@ 别名能解析"。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.ts'],
    // 兜住 Node 自带 localStorage 盖住 jsdom 实现的坑（见 setup.ts）
    setupFiles: ['src/test/setup.ts'],
    // 组件测试里断言 DOM 结构，不需要快照
    restoreMocks: true,
  },
})
