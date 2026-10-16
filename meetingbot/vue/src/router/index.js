import { createRouter, createWebHistory } from 'vue-router'

import Login from '@/views/Login.vue'
import Register from '@/views/Register.vue'
import FourOFour from '@/views/404.vue'

import Home from '@/views/manager/Home.vue'
import User from '@/views/manager/User.vue'
import Department from '@/views/manager/Department.vue'
import DepartmentMember from '@/views/manager/DepartmentMember.vue'
import Meeting from '@/views/manager/Meeting.vue'
import MeetingMaterial from '@/views/manager/MeetingMaterial.vue'
import Transcription from '@/views/manager/Transcription.vue'
import MeetingMinutes from '@/views/manager/MeetingMinutes.vue'
import Agent from '@/views/manager/Agent.vue'
import Observability from '@/views/manager/Observability.vue'
import AiModel from '@/views/manager/AiModel.vue'
import PromptTemplate from '@/views/manager/PromptTemplate.vue'
import Person from '@/views/manager/Person.vue'
import Password from '@/views/manager/Password.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/login' },
    {
      path: '/manager',
      component: () => import('@/views/Manager.vue'),
      children: [
        // /manager 根路径默认进首页（此前无默认子路由，直接访问内容区空白）
        { path: '', redirect: 'home' },
        { path: 'home', meta: { name: '系统首页' }, component: Home,  },
        { path: 'user', meta: { name: '用户管理', requiresAdmin: true }, component: User, },
        { path: 'department', meta: { name: '部门管理', requiresAdmin: true }, component: Department, },
        { path: 'departmentMembers', meta: { name: '部门成员', requiresAdmin: true }, component: DepartmentMember, },
        { path: 'meeting', meta: { name: '会议管理' }, component: Meeting, },
        { path: 'meetingMaterial', meta: { name: '会议资料' }, component: MeetingMaterial, },
        { path: 'transcription', meta: { name: '转写任务' }, component: Transcription, },
        { path: 'meetingMinutes', meta: { name: '会议纪要' }, component: MeetingMinutes, },
        { path: 'agent', meta: { name: '纪要自检' }, component: Agent, },
        { path: 'observability', meta: { name: '运行观测' }, component: Observability, },
        { path: 'aiModel', meta: { name: 'AI模型配置', requiresAdmin: true }, component: AiModel, },
        { path: 'promptTemplate', meta: { name: 'Prompt模板配置', requiresAdmin: true }, component: PromptTemplate, },
        { path: 'person', meta: { name: '个人资料' }, component: Person, },
        { path: 'password', meta: { name: '修改密码' }, component: Password, },
        // 新路由
      ]
    },
    { path: '/login', component: Login },
    { path: '/register', component: Register },
    { path: '/404', component: FourOFour },
    { path: '/:pathMatch(.*)', redirect: '/404' }
  ]
})

// 切换页面跳转到顶部
router.afterEach(() => {
  setTimeout(() => {
    window.scroll({ top: 0, behavior: 'smooth' })
  }, 0)
})

router.beforeEach((to) => {
  // 从登录缓存读当前用户
  const user = JSON.parse(localStorage.getItem('xm-user') || '{}')
  // 进入管理端任何页面都要求已登录，缓存里没有 id 时打回登录页
  if (to.path.startsWith('/manager') && !user.id) {
    return '/login'
  }
  // 目标页面标了 requiresAdmin 而当前用户不是管理员时，跳到首页
  if (to.meta.requiresAdmin && user.role !== 'ADMIN') {
    return '/manager/home'
  }
})

export default router
