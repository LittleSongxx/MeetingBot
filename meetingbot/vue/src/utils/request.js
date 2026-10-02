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
// 可以自请求发送前对请求做一些处理
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
                ElMessage.error(msg)
                // 401 说明 Token 缺失、过期或账号已停用，回到登录页重新登录
                if (result.code === '401') {
                    router.push('/login')
                }
                return Promise.reject(new Error(msg))
            })
        }
        // 当权限验证不通过的时候给出提示
        // 401 由 get_current_user 抛出，说明 Token 缺失、过期或账号已停用
        if (res.code === '401') {
            ElMessage.error(res.msg)
            router.push('/login')
        }
        // 兼容服务端返回的字符串数据
        if (typeof res === 'string') {
            res = res ? JSON.parse(res) : res
        }
        // 把响应体直接返回，页面里的 res 就是 { code, msg, data }
        return res;
    },
    error => {
        // 走到这里说明 HTTP 状态码本身不是 2xx，后端的统一异常处理没有生效
        if (error.response.status === 404) {
            ElMessage.error('未找到请求接口')
        } else if (error.response.status === 500) {
            ElMessage.error('系统异常，请查看后端控制台报错')
        } else {
            console.error(error.message)
        }
        return Promise.reject(error)
    }
)

export default request
