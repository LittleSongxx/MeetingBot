// 后端返回的头像等资源是 /files/download/xxx 这样的相对路径，
// 浏览器直接拿它当 src 会被解析成"当前前端站点"下的地址，所以要统一拼一次。
//
// VITE_BASE_URL 在两个环境里的含义不同：
// - 开发环境是 http://localhost:9090，直接取它的 origin 就是后端地址；
// - 生产环境是同源部署的 /api（nginx 把 /api/ 反代到后端），没有 origin，
//   就取当前页面的 origin，也就是 nginx 自己的地址。
const getApiOrigin = () => {
    const baseUrl = import.meta.env.VITE_BASE_URL || ''
    // 带协议头的绝对地址（如 http://localhost:9090）取它的 origin
    if (/^https?:\/\//i.test(baseUrl)) {
        return new URL(baseUrl).origin
    }
    // 相对地址说明前后端同源，用当前页面的 origin
    return typeof window === 'undefined' ? '' : window.location.origin
}

// 把后端返回的相对资源地址转换成浏览器可直接访问的地址
// 已经是 http(s)、协议相对（//cdn.xxx）、blob: 或 data: 的地址原样返回
export const resolveAssetUrl = path => {
    // 空值（例如用户没上传头像）返回空字符串，交给页面显示默认占位
    if (!path) {
        return ''
    }
    const value = String(path)
    if (/^(?:https?:)?\/\//i.test(value) || /^(?:blob|data):/i.test(value)) {
        return value
    }
    // 后端返回的路径一般以 / 开头，缺少时补一个，保证拼出来的是绝对路径
    const normalizedPath = value.startsWith('/') ? value : '/' + value
    return getApiOrigin() + normalizedPath
}
