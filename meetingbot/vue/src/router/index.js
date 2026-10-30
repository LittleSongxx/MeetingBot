import { createRouter, createWebHistory } from 'vue-router'

import { readUser } from '@/utils/storage.js'

// 生产环境关闭注册入口（.env.production 里 VITE_REGISTER_ENABLED=false），
// 直接访问 /register 也由守卫打回登录页，避免露出一个注定提交失败的注册表单
const registerEnabled = import.meta.env.VITE_REGISTER_ENABLED === 'true'

// 全部页面改为路由级懒加载：首屏只下载登录/管理端框架，其余页面按访问拆成独立 chunk，
// Observability 这类带 ECharts 的重页面不会再打进首屏包
const routes = [
  { path: '/', redirect: '/login' },
  {
    path: '/manager',
    component: () => import('@/views/Manager.vue'),
    children: [
      { path: 'home', meta: { name: '系统首页' }, component: () => import('@/views/manager/Home.vue') },
      { path: 'user', meta: { name: '用户管理', requiresAdmin: true }, component: () => import('@/views/manager/User.vue') },
      { path: 'department', meta: { name: '部门管理', requiresAdmin: true }, component: () => import('@/views/manager/Department.vue') },
      { path: 'departmentMembers', meta: { name: '部门成员' }, component: () => import('@/views/manager/DepartmentMember.vue') },
      { path: 'meeting', meta: { name: '会议管理' }, component: () => import('@/views/manager/Meeting.vue') },
      { path: 'meetingMaterial', meta: { name: '会议资料' }, component: () => import('@/views/manager/MeetingMaterial.vue') },
      { path: 'transcription', meta: { name: '转写任务' }, component: () => import('@/views/manager/Transcription.vue') },
      { path: 'meetingMinutes', meta: { name: 'AI会议纪要' }, component: () => import('@/views/manager/MeetingMinutes.vue') },
      { path: 'actionItems', meta: { name: '行动项' }, component: () => import('@/views/manager/ActionItems.vue') },
      { path: 'agent', meta: { name: '纪要自检' }, component: () => import('@/views/manager/Agent.vue') },
      { path: 'observability', meta: { name: '运行观测' }, component: () => import('@/views/manager/Observability.vue') },
      { path: 'aiModel', meta: { name: 'AI模型配置', requiresAdmin: true }, component: () => import('@/views/manager/AiModel.vue') },
      { path: 'promptTemplate', meta: { name: 'Prompt模板配置', requiresAdmin: true }, component: () => import('@/views/manager/PromptTemplate.vue') },
      { path: 'person', meta: { name: '个人资料' }, component: () => import('@/views/manager/Person.vue') },
      { path: 'password', meta: { name: '修改密码' }, component: () => import('@/views/manager/Password.vue') },
    ]
  },
  { path: '/login', meta: { title: '登录' }, component: () => import('@/views/Login.vue') },
  { path: '/register', meta: { title: '注册' }, component: () => import('@/views/Register.vue') },
  { path: '/404', meta: { title: '页面不存在' }, component: () => import('@/views/404.vue') },
  { path: '/:pathMatch(.*)', redirect: '/404' }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

// 切换页面：设置浏览器标签标题 + 回到顶部
router.afterEach((to) => {
  const title = to.meta?.title || to.meta?.name
  document.title = title ? `${title} · MeetingBot` : 'MeetingBot · AI 智能会议纪要辅助系统'
  setTimeout(() => {
    window.scroll({ top: 0, behavior: 'smooth' })
  }, 0)
})

router.beforeEach((to) => {
  // 从登录缓存读当前用户；缓存损坏时 readUser 会清掉脏数据并返回空对象
  const user = readUser()
  // 生产环境关闭注册：入口已隐藏，直接输入 /register 也重定向回登录页
  if (to.path === '/register' && !registerEnabled) {
    return { path: '/login', replace: true }
  }
  // 进入管理端任何页面都要求已登录，缓存里没有 id 时打回登录页，
  // 并把原地址放进 redirect，登录成功后可以跳回
  if (to.path.startsWith('/manager') && !user.id) {
    const redirect = to.fullPath && to.fullPath !== '/manager' ? { redirect: to.fullPath } : {}
    return { path: '/login', query: redirect }
  }
  // 目标页面标了 requiresAdmin 而当前用户不是管理员时，跳到首页
  if (to.meta.requiresAdmin && user.role !== 'ADMIN') {
    return '/manager/home'
  }
})

export default router
