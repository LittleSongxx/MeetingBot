/**
 * 测试环境兜底：localStorage。
 *
 * Node 25 自己暴露了一个 localStorage 全局（要用 --localstorage-file 才真正可用），
 * 它会盖住 jsdom 的实现，于是测试里 `localStorage.setItem is not a function`。
 * 这里在发现它不可用时补一个内存实现——项目里 localStorage 只用来放登录态，
 * 内存实现与浏览器行为的差别只在于刷新后不保留，测试不需要那点差别。
 */
if (typeof globalThis.localStorage?.setItem !== 'function') {
  const store = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
      setItem: (key: string, value: string) => void store.set(key, String(value)),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => Array.from(store.keys())[index] ?? null,
      get length() {
        return store.size
      },
    },
  })
}
