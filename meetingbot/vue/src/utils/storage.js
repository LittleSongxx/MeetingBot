// 登录用户缓存（localStorage['xm-user']）的统一读写入口。
// 缓存可能被手工改坏或来自旧版本格式，JSON.parse 一旦抛错会让路由守卫/请求拦截器
// 直接中断并白屏，所以这里统一 try/catch：解析失败就清掉缓存，当作未登录处理。

const USER_KEY = 'xm-user'

// 读取当前登录用户，读不到或格式非法时返回空对象（调用方按未登录处理）
export const readUser = () => {
    try {
        const raw = localStorage.getItem(USER_KEY)
        if (!raw) {
            return {}
        }
        const user = JSON.parse(raw)
        return user && typeof user === 'object' ? user : {}
    } catch (e) {
        // 缓存损坏：清掉脏数据，让路由守卫把用户送回登录页
        try {
            localStorage.removeItem(USER_KEY)
        } catch (ignored) {
            // 浏览器禁用 localStorage 时忽略，页面仍可按未登录处理
        }
        return {}
    }
}

// 写入登录用户，localStorage 不可用（隐私模式/配额满）时不阻塞登录流程
export const writeUser = user => {
    try {
        localStorage.setItem(USER_KEY, JSON.stringify(user || {}))
    } catch (e) {
        // 忽略写入失败
    }
}

// 清除登录态：登出、401、修改密码后统一调用
export const clearUser = () => {
    try {
        localStorage.removeItem(USER_KEY)
    } catch (e) {
        // 忽略
    }
}

// 取请求头里的 Token，未登录或缓存损坏时返回空串
export const getToken = () => readUser().token || ''
