<template>
  <div class="auth-container">
    <!-- 左半屏和登录页保持一致，注册进来的人先看清这套系统是做什么的 -->
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
      <div class="auth-foot">企业内部系统；如未开放自助注册，请联系管理员开通账号</div>
    </div>

    <!-- 右半屏：注册表单 -->
    <div class="auth-form-side">
      <div class="auth-form-box">
        <div class="auth-title">注册</div>
        <div class="auth-tip">注册后是普通员工账号，所属部门和职位由管理员分配</div>
        <!-- 表单内按回车也执行 login，与“注册”按钮走同一套校验和请求 -->
        <el-form ref="formRef" :model="data.form" :rules="data.rules" @keyup.enter="login">
          <!-- 账号输入框。输入值写进 data.form.username，提交时作为注册请求体的 username -->
          <el-form-item prop="username">
            <el-input :prefix-icon="User" size="large" v-model="data.form.username" placeholder="账号"></el-input>
          </el-form-item>
          <!-- 密码输入框。show-password 让密码默认掩码显示，输入值写进 data.form.password -->
          <el-form-item prop="password">
            <el-input show-password :prefix-icon="Lock" size="large" v-model="data.form.password" placeholder="密码"></el-input>
          </el-form-item>
          <!-- 确认密码输入框。输入值写进 data.form.confirmPassword，前端 validatePass 和后端 register 都拿它跟密码比对 -->
          <el-form-item prop="confirmPassword">
            <el-input show-password :prefix-icon="Lock" size="large" v-model="data.form.confirmPassword" placeholder="确认密码"></el-input>
          </el-form-item>
          <!-- 注册按钮。点击后执行 login 方法，方法内部先触发表单校验再发 POST /register；data.loading 为 true 时按钮转圈 -->
          <el-form-item>
            <el-button size="large" type="primary" style="width: 100%" :loading="data.loading" @click="login">注册</el-button>
          </el-form-item>
        </el-form>
        <div class="auth-switch">
          <!-- 表单下方的切换链接，已有账号的用户点“去登录”回到 /login -->
          已有账号？<a @click="router.push('/login')">去登录</a>
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

// 左侧展示的会议处理链路，和登录页保持同一份内容
const steps = [
  { no: '01', text: '上传会议录音或视频' },
  { no: '02', text: '自动转写并区分说话人' },
  { no: '03', text: '生成结构化的会议纪要' },
  { no: '04', text: 'Agent 对照原文自检修订' },
  { no: '05', text: '人工确认后导出归档' }
]

// 确认密码的自定义校验函数。value 是确认密码输入框的当前值，callback 用来回传校验结果
const validatePass = (rule, value, callback) => {
  if (!value) {
    // 确认密码为空时判定不通过，页面在输入框下方显示这句提示
    callback(new Error('请确认密码'))
  } else {
    // 拿确认密码和密码输入框的值比对，不一致时提示
    if (value !== data.form.password) {
      callback(new Error("确认密码跟原密码不一致!"))
    }
    callback()
  }
}
const data = reactive({
  // 三个输入框共同绑定这个对象，注册请求会把它整体作为请求体提交
  form: { },
  // 注册按钮绑定此字段，请求发出到 finally 完成期间显示加载状态
  loading: false,
  // 点击注册按钮时由 el-form 逐条执行的校验规则
  rules: {
    // 账号非空校验，输入框失焦时触发
    username: [
      { required: true, message: '请输入账号', trigger: 'blur' }
    ],
    // 密码非空 + 最短长度校验（与后端 register 的 6 位下限同口径）
    password: [
      { required: true, message: '请输入密码', trigger: 'blur' },
      { min: 6, message: '密码至少需要6位', trigger: 'blur' }
    ],
    // 确认密码使用上面的自定义函数校验，输入框失焦时触发
    confirmPassword: [
        { validator: validatePass, trigger: 'blur' }
    ]
  }
})

// 指向模板上的 el-form 实例，login 方法通过它调用 validate 做整体校验
const formRef = ref()

const login = () => {
  // 点击“注册”按钮或在表单内按回车都会进入这里，先按 data.rules 校验三个输入框
  formRef.value.validate(valid => {
    // 表单校验通过后才提交输入框中的账号或密码
    if (valid) {
      // 打开“注册”按钮的加载状态，防止请求返回前重复点击
      data.loading = true
      // data.form 里有 username、password、confirmPassword 三个字段，整体作为 JSON 请求体发给 POST /register
      request.post('/register', data.form).then(res => {
        // code 为 '200' 说明后端 register 已向 user 表插入新账号
        if (res.code === '200') {
          ElMessage.success('注册成功')
          // 注册接口不返回 Token，页面不写 xm-user，直接跳到登录页让用户用新账号登录
          router.push('/login')
        } else {
          // 例如“两次输入的密码不一致”“账号重复”，直接弹出后端给的提示
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
