import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import '@/assets/css/global.css'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

// 请求失败已经由 utils/request.js 统一弹过提示，页面大多只写 .then 不写 .catch，
// 浏览器会把被拒绝的 Promise 再当成 Uncaught (in promise) 打到控制台；
// 拦截器给这类错误打了 isHandled 标记，这里直接吞掉，避免重复报错。
window.addEventListener('unhandledrejection', event => {
    if (event.reason && event.reason.isHandled) {
        event.preventDefault()
    }
})

const app = createApp(App)

app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')

import * as ElementPlusIconsVue from '@element-plus/icons-vue'

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
}
