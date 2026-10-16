import axios from "axios";
import {ElMessage} from "element-plus";
import router from "@/router/index.js";

const request = axios.create({
    // 后端地址来自 ai_meeting/vue/.env.development 里的 VITE_BASE_URL
    baseURL: import.meta.env.VITE_BASE_URL,
    // 普通后台接口超时时间
    timeout: 30000
})

// request 拦截器
request.interceptors.request.use(config => {
    // FormData 由浏览器自动补全 multipart boundary，其他请求继续使用 JSON。
    if (config.data instanceof FormData) {
        delete config.headers['Content-Type']
    } else {
        config.headers['Content-Type'] = 'application/json;charset=utf-8';
    }
    // 从登录时写入的 xm-user 缓存里取出当前登录用户
    let user = JSON.parse(localStorage.getItem("xm-user") || '{}')
    // 把 Token 放进名为 token 的请求头，后端 get_current_user 就是从这个头读取的
    config.headers['token'] = user.token || ''
    return config
}, error => {
    return Promise.reject(error)
});

// ---------- 401 统一处理 ----------
// 三个动作必须只做一次：清掉失效登录态（否则路由守卫仍放行，造成 401 循环弹跳）、
// 去重跳转（首页并发 7 个请求过期时会触发 7 次 push + 7 条 toast）、
// 返回悬挂 promise（让页面 .then 不再把错误响应当数据用，也不再双弹）。
let redirectingToLogin = false
function handleUnauthorized(msg) {
    if (!redirectingToLogin) {
        redirectingToLogin = true
        ElMessage.error(msg || '登录状态已失效，请重新登录')
        localStorage.removeItem('xm-user')
        router.push('/login').finally(() => {
            // 跳转完成后恢复，允许下一次会话过期再触发
            redirectingToLogin = false
        })
    }
    return new Promise(() => {})  // 悬挂：过期会话的后续链路不再执行
}

// response 拦截器
request.interceptors.response.use(
    response => {
        let res = response.data;
        // 如果是返回的文件
        // 第 6 章的资料下载与预览、第 12 章的纪要导出都用 responseType: 'blob'，这类响应原样交给调用方处理
        if (response.config.responseType === 'blob') {
            return res
        }
        // 兼容服务端返回的字符串数据
        if (typeof res === 'string') {
            res = res ? JSON.parse(res) : res
        }
        // 200 响应体里的 401（旧后端兼容路径）：与 HTTP 401 走同一处理
        if (res && res.code === '401') {
            return handleUnauthorized(res.msg)
        }
        // 业务失败（code != 200）：统一弹一次提示并 reject，页面不必再各自 else 弹错
        if (res && res.code && res.code !== '200') {
            ElMessage.error(res.msg || '操作失败')
            return Promise.reject(res)
        }
        // 把响应体直接返回，页面里的 res 就是 { code, msg, data }
        return res;
    },
    error => {
        // error.response 缺失 = 请求根本没到响应阶段：断网、DNS、CORS、超时、后端未启动。
        // 此前直接访问 error.response.status 会把拦截器自己抛 TypeError，用户什么都看不到。
        if (!error.response) {
            if (error.code === 'ECONNABORTED') {
                ElMessage.error('请求超时，请稍后重试')
            } else {
                ElMessage.error('网络连接失败或服务不可用，请检查网络后重试')
            }
            return Promise.reject(error)
        }
        const status = error.response.status
        // 后端现在返回真实状态码，body 仍是 { code, msg }
        const body = error.response.data
        const msg = (body && body.msg) || ''
        if (status === 401) {
            return handleUnauthorized(msg)
        }
        if (status === 404) {
            ElMessage.error('未找到请求接口')
        } else if (status === 403) {
            ElMessage.error(msg || '没有操作权限')
        } else if (status === 422) {
            ElMessage.error(msg || '请求参数错误')
        } else if (status >= 500) {
            ElMessage.error(msg || '系统异常，请稍后重试')
        } else {
            // 400 及其他：后端业务错误带 msg；没有 msg 时给通用文案
            ElMessage.error(msg || '操作失败')
        }
        return Promise.reject(error)
    }
)

export default request
