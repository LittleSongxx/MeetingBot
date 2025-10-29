<template>
  <div class="manager-container">
    <div class="manager-header">
      <div class="manager-header-left">
        <img src="@/assets/imgs/logo.png" alt="">
        <div class="title">MeetingBot</div>
      </div>
      <div class="manager-header-center">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/manager/home' }">首页</el-breadcrumb-item>
          <el-breadcrumb-item>{{ router.currentRoute.value.meta.name }}</el-breadcrumb-item>
        </el-breadcrumb>
      </div>
      <div class="manager-header-right">
        <el-dropdown style="cursor: pointer">
          <div style="padding-right: 20px; display: flex; align-items: center">
            <!-- 头像地址取自 data.user.avatar，也就是 user 表的 avatar 字段 -->
            <img style="width: 32px; height: 32px; border-radius: 50%;" :src="fileUrl(data.user.avatar)" alt="">
            <!-- 姓名取自 data.user.name -->
            <span style="margin-left: 8px; color: #1f2d3d">{{ data.user.name }}</span><el-icon color="#8a94a6" style="margin-left: 4px"><arrow-down /></el-icon>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <!-- 跳转到本章的个人资料页 -->
              <el-dropdown-item @click="router.push('/manager/person')">个人资料</el-dropdown-item>
              <!-- 跳转到本章的修改密码页 -->
              <el-dropdown-item @click="router.push('/manager/password')">修改密码</el-dropdown-item>
              <!-- 退出登录，清掉本地缓存 -->
              <el-dropdown-item @click="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>
    <!-- 下面部分开始 -->
    <div style="display: flex">
      <div class="manager-main-left">
        <el-menu :default-active="router.currentRoute.value.path"
                 :default-openeds="['1', '2', '3', '4', '5', '6']"
                 router
        >
          <el-menu-item index="/manager/home">
            <el-icon><HomeFilled /></el-icon>
            <span>系统首页</span>
          </el-menu-item>
          <el-sub-menu index="1">
            <template #title>
              <el-icon><Calendar /></el-icon>
              <span>会议中心</span>
            </template>
            <el-menu-item index="/manager/meeting">会议管理</el-menu-item>
            <el-menu-item index="/manager/meetingMaterial">会议资料</el-menu-item>
            <el-menu-item index="/manager/transcription">转写任务</el-menu-item>
            <el-menu-item index="/manager/meetingMinutes">会议纪要</el-menu-item>
          </el-sub-menu>
          <el-sub-menu index="2">
            <template #title>
              <el-icon><Cpu /></el-icon>
              <span>智能助手</span>
            </template>
            <el-menu-item index="/manager/agent">纪要自检</el-menu-item>
            <el-menu-item index="/manager/observability">运行观测</el-menu-item>
          </el-sub-menu>
          <!-- v-if 加在 el-sub-menu 上，员工登录时整个分组连标题一起不渲染 -->
          <el-sub-menu v-if="data.user.role === 'ADMIN'" index="3">
            <template #title>
              <el-icon><Setting /></el-icon>
              <span>系统配置</span>
            </template>
            <el-menu-item index="/manager/aiModel">AI模型配置</el-menu-item>
            <el-menu-item index="/manager/promptTemplate">Prompt模板配置</el-menu-item>
            <!-- 新菜单 -->
          </el-sub-menu>
          <el-sub-menu index="4">
            <template #title>
              <el-icon><Menu /></el-icon>
              <span>组织与员工</span>
            </template>
            <!-- 第 2 章的部门管理，只有管理员能看到 -->
            <el-menu-item v-if="data.user.role === 'ADMIN'" index="/manager/department">部门管理</el-menu-item>
            <!-- 第 2 章的员工账号管理，只有管理员能看到 -->
            <el-menu-item v-if="data.user.role === 'ADMIN'" index="/manager/user">员工账号管理</el-menu-item>
            <!-- 第 2 章的部门成员页，只有员工能看到 -->
            <el-menu-item v-if="data.user.role === 'EMPLOYEE'" index="/manager/departmentMembers">所在部门成员</el-menu-item>
          </el-sub-menu>
        </el-menu>
      </div>
      <div class="manager-main-right">
        <!-- 子页面通过 emit('updateUser') 通知框架页重新读缓存 -->
        <RouterView @updateUser="updateUser" />
      </div>
    </div>
    <!-- 下面部分结束 -->


  </div>
</template>

<script setup>
import { reactive } from "vue";
import router from "@/router/index.js";
import { fileUrl } from "@/utils/fileUrl.js";
import {ElMessage} from "element-plus";

const data = reactive({
  // 第 1 章登录时写入的 xm-user 缓存，顶部头像、姓名和左侧菜单的角色判断都读它
  user: JSON.parse(localStorage.getItem('xm-user') || '{}')
})

const logout = () => {
  // 清掉登录缓存，请求拦截器之后取到的 token 就是空串
  localStorage.removeItem('xm-user')
  router.push('/login')
}

const updateUser = () => {
  // 重新从 localStorage 读一次，顶部头像和姓名随之重新渲染
  data.user =  JSON.parse(localStorage.getItem('xm-user') || '{}')
}

if (!data.user.id) {
  logout()
  ElMessage.error('请登录！')
}
</script>

<style scoped>
@import "@/assets/css/manager.css";
</style>
