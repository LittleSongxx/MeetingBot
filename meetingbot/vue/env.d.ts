/// <reference types="vite/client" />

// 让 `import Foo from './Foo.vue'` 有类型：Vue SFC 是文件类型，不是模块类型
declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

// 环境变量的类型（与 .env.* 里实际用到的键一一对应，多写一个键就是撒谎）
interface ImportMetaEnv {
  readonly VITE_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
