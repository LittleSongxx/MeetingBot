<template>
  <div class="auth-container">
    <!-- 左半屏：说明这套系统把一场会议处理成什么，五个步骤对应系统里的五个页面 -->
    <div class="auth-intro">
      <div class="auth-brand">
        <img src="@/assets/imgs/logo.png" alt="">
        <span>MeetingBot · 智能会议纪要辅助系统</span>
      </div>
      <div>
        <div class="auth-headline">把一场会议，变成一份能用的纪要</div>
        <div class="auth-subline">
          录音上传之后，系统会自动转写、区分说话人、整理成结构化纪要，再由 Agent 对着原文自检一遍，改完交给你确认。
        </div>
        <div class="auth-steps">
          <div class="auth-step" v-for="item in steps" :key="item.no">
            <div class="auth-step-no">{{ item.no }}</div>
            <div class="auth-step-text">{{ item.text }}</div>
          </div>
        </div>
      </div>
      <div class="auth-foot">企业内部系统，账号由管理员统一分配</div>
    </div>

    <!-- 右半屏：登录表单 -->
    <div class="auth-form-side">
      <div class="auth-form-box">
        <div class="auth-title">登录</div>
        <div class="auth-tip">请输入你的账号和密码</div>
        <!-- 表单内按回车也执行 login，与提交按钮使用同一套校验和请求 -->
        <el-form ref="formRef" :model="data.form" :rules="data.rules" @keyup.enter="login">
          <!-- 账号输入框。输入值写进 data.form.username，提交时作为登录请求体的 username -->
          <el-form-item prop="username">
            <el-input :prefix-icon="User" size="large" v-model="data.form.username" placeholder="账号"></el-input>
          </el-form-item>
          <!-- 密码输入框。输入值写进 data.form.password，提交时作为登录请求体的 password -->
          <el-form-item prop="password">
            <el-input show-password :prefix-icon="Lock" size="large" v-model="data.form.password" placeholder="密码"></el-input>
          </el-form-item>
          <!-- 登录按钮。点击后执行 login 方法，方法内部先触发表单校验再发 POST /login；data.loading 为 true 时按钮转圈 -->
          <el-form-item>
            <el-button size="large" type="primary" style="width: 100%" :loading="data.loading" @click="login">登录</el-button>
          </el-form-item>
        </el-form>
        <div class="auth-switch">
          <!-- 表单下方的切换链接，没有账号的员工点“注册一个”进入 /register -->
          还没有账号？<a @click="router.push('/register')">注册一个</a>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from "vue";
import { User, Lock } from "@element-plus/icons-vue";
import request from "@/utils/request.js";
import {ElMessage} from "element-plus";
import router from "@/router/index.js";

// 左侧展示的会议处理链路，每一步都对应管理端的一个菜单
const steps = [
  { no: '01', text: '上传会议录音或视频' },
  { no: '02', text: '自动转写并区分说话人' },
  { no: '03', text: '生成结构化的会议纪要' },
  { no: '04', text: 'Agent 对照原文自检修订' },
  { no: '05', text: '人工确认后导出归档' }
]

const data = reactive({
  // 账号和密码两个输入框共同绑定这个对象，登录请求会把它整体作为请求体提交
  form: {},
  // 登录按钮绑定此字段，请求发出到 finally 完成期间显示加载状态
  loading: false,
  // 点击登录按钮时由 el-form 逐条执行的校验规则
  rules: {
    // 账号非空校验，输入框失焦时触发
    username: [
      { required: true, message: '请输入账号', trigger: 'blur' }
    ],
    // 密码非空校验，输入框失焦时触发
    password: [
      { required: true, message: '请输入密码', trigger: 'blur' }
    ]
  }
})

// 指向模板上的 el-form 实例，login 方法通过它调用 validate 做整体校验
const formRef = ref()

const login = () => {
  // 点击“登录”按钮或在表单内按回车都会进入这里，先按 data.rules 校验账号和密码是否为空
  formRef.value.validate(valid => {
    // 表单校验通过后才提交输入框中的账号或密码
    if (valid) {
      // 打开“登录”按钮的加载状态，防止请求返回前重复点击
      data.loading = true
      // data.form 里的 username、password 作为 JSON 请求体发给 POST /login
      request.post('/login', data.form).then(res => {
        // code 为 '200' 说明后端 login 已校验通过，res.data 是 account_dict 组装的用户信息和 token
        if (res.code === '200') {
          ElMessage.success('登录成功')
          // 存储用户信息到浏览器的缓存
          localStorage.setItem('xm-user', JSON.stringify(res.data))
          // 进入管理端首页，Manager.vue 从 xm-user 读取 role 渲染菜单
          router.push('/manager/home')
        } else {
          // 例如“账号或密码错误”“账号已被停用”，直接弹出后端给的提示
          ElMessage.error(res.msg)
        }
      }).finally(() => {
        // 请求成功或失败都关闭按钮加载状态，按钮恢复可点击
        data.loading = false
      })
    }
  })
}
</script>

<style scoped>
@import "@/assets/css/auth.css";
</style>
