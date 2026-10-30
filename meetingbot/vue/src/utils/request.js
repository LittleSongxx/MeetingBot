import axios from "axios";
import {ElMessage} from "element-plus";
import router from "@/router/index.js";
import {clearUser, getToken} from "@/utils/storage.js";

const request = axios.create({
    // 后端地址来自 ai_meeting/vue/.env.development 里的 VITE_BASE_URL
    baseURL: import.meta.env.VITE_BASE_URL,
    // 普通后台接口超时时间
    timeout: 30000
})

// 最近一条错误提示，用来合并并发请求的同一句提示，避免一次断网弹出十几条 toast
let lastError = {message: '', time: 0}
const showError = message => {
    const now = Date.now()
    if (message === lastError.message && now - lastError.time < 1500) {
        return
    }
    lastError = {message, time: now}
    ElMessage.error(message)
}

// 给"已经在拦截器里提示过"的错误打标记，页面不写 catch 时 main.js 会忽略它的
// unhandledrejection，避免浏览器控制台再报一条 Uncaught (in promise)
const markHandled = error => {
    const result = error instanceof Error ? error : new Error(String(error || '请求失败'))
    result.isHandled = true
    return result
}

// 401 是全局登录态问题：并发请求同时失效时只提示、只跳转一次，并清掉本地登录缓存
let handlingUnauthorized = false
const handleUnauthorized = msg => {
    // 无论是否已经处理过，都先把失效的登录态清掉，避免路由守卫继续放行
    clearUser()
    if (handlingUnauthorized) {
        return
    }
    handlingUnauthorized = true
    ElMessage.error(msg || '登录状态已失效，请重新登录')
    const current = router.currentRoute.value
    const redirect = current.path && current.path.startsWith('/manager') ? current.fullPath : undefined
    if (current.path !== '/login') {
        router.push(redirect ? {path: '/login', query: {redirect}} : {path: '/login'}).catch(() => {})
    }
    // 留出一小段时间窗口，把同一批并发请求的重复 401 合并掉
    window.setTimeout(() => {
        handlingUnauthorized = false
    }, 1500)
}

// request 拦截器
// 可以自请求发送前对请求做一些处理
request.interceptors.request.use(config => {
    // FormData 由浏览器自动补全 multipart boundary，其他请求继续使用 JSON。
    if (config.data instanceof FormData) {
        delete config.headers['Content-Type']
    } else {
        config.headers['Content-Type'] = 'application/json;charset=utf-8';
    }
    // 把 Token 放进名为 token 的请求头，后端 get_current_user 就是从这个头读取的
    config.headers['token'] = getToken()
    return config
}, error => {
    return Promise.reject(error)
});

// 判断一个 Blob 是不是后端返回的 JSON 错误体
// 后端统一异常处理始终以 HTTP 200 + { code, msg } 返回，所以 blob 请求失败时
// 拿到的其实是一个 application/json 的 Blob，而不是真正的文件
const isJsonBlob = blob => blob instanceof Blob && blob.type.includes('json')

// 把 JSON 错误体解析出来，失败时给一句兜底提示
const readBlobError = async blob => {
    try {
        const result = JSON.parse(await blob.text())
        return result && typeof result === 'object' ? result : {}
    } catch (e) {
        return {}
    }
}

// response 拦截器
// 可以在接口响应后统一处理结果
request.interceptors.response.use(
    response => {
        let res = response.data;
        // 如果是返回的文件
        // 第 6 章的资料下载与预览、第 12 章的纪要导出都用 responseType: 'blob'，这类响应原样交给调用方处理
        if (response.config.responseType === 'blob') {
            // 正常情况就是文件内容，直接交给调用方生成下载链接
            if (!isJsonBlob(res)) {
                return res
            }
            // 后端抛业务异常时返回的是 JSON，解析出提示后拒绝这次请求，
            // 避免调用方把一个错误提示当成文件保存下来
            return readBlobError(res).then(result => {
                const msg = result.msg || '文件处理失败'
                // 401 说明 Token 缺失、过期或账号已停用，清缓存并回到登录页重新登录
                if (result.code === '401') {
                    handleUnauthorized(msg)
                    return Promise.reject(markHandled(new Error(msg)))
                }
                ElMessage.error(msg)
                return Promise.reject(markHandled(new Error(msg)))
            })
        }
        // 当权限验证不通过的时候给出提示
        // 401 由 get_current_user 抛出，说明 Token 缺失、过期或账号已停用
        if (res && res.code === '401') {
            handleUnauthorized(res.msg)
            // 拒绝这次请求，页面 .then 里的 else 分支不会重复弹同一句提示
            return Promise.reject(markHandled(new Error(res.msg || '登录状态已失效，请重新登录')))
        }
        // 兼容服务端返回的字符串数据
        if (typeof res === 'string') {
            res = res ? JSON.parse(res) : res
        }
        // 把响应体直接返回，页面里的 res 就是 { code, msg, data }
        return res;
    },
    error => {
        // 主动取消的请求（路由切换/组件卸载）不提示
        if (axios.isCancel?.(error) || error.code === 'ERR_CANCELED') {
            return Promise.reject(error)
        }
        // 走到这里说明 HTTP 状态码本身不是 2xx，后端的统一异常处理没有生效；
        // error.response 为空代表网络中断、DNS 失败或请求超时，不能再直接读 .status
        const status = error.response?.status
        let message = '网络异常，请检查网络连接'
        if (status === 404) {
            message = '未找到请求接口'
        } else if (status === 500) {
            message = '系统异常，请查看后端控制台报错'
        } else if (status) {
            message = `请求失败（${status}）`
        } else if (error.code === 'ECONNABORTED' || /timeout/i.test(error.message || '')) {
            message = '请求超时，请稍后重试'
        }
        showError(message)
        return Promise.reject(markHandled(error))
    }
)

export default request
