<template>
  <div style="width: 50%" class="card">
    <el-form ref="formRef" :rules="data.rules" :model="data.user" label-width="80px" style="padding: 20px">
      <!-- 原密码输入框，写入 data.user.password，后端拿它和数据库里的密码比对 -->
      <el-form-item label="原密码" prop="password">
        <el-input v-model="data.user.password" placeholder="请输入原密码" show-password></el-input>
      </el-form-item>
      <!-- 新密码输入框，写入 data.user.newPassword，通过校验后写进 user.password -->
      <el-form-item label="新密码" prop="newPassword">
        <el-input v-model="data.user.newPassword" placeholder="请输入新密码" show-password></el-input>
      </el-form-item>
      <!-- 确认密码输入框，写入 data.user.confirmPassword，只在前端和新密码比对 -->
      <el-form-item label="确认密码" prop="confirmPassword">
        <el-input v-model="data.user.confirmPassword" placeholder="请确认新密码" show-password></el-input>
      </el-form-item>
      <div style="text-align: center">
        <!-- 保存按钮，先跑三条校验规则再提交 -->
        <el-button type="primary" @click="updatePassword">保 存</el-button>
      </div>
    </el-form>
  </div>
</template>

<script setup>
import {reactive, ref} from "vue";
import request from "@/utils/request.js";
import {ElMessage} from "element-plus";
import router from "@/router/index.js";

const formRef = ref()

// 确认密码的自定义校验函数。value 是确认密码输入框的当前值
const validatePass = (rule, value, callback) => {
  if (!value) {
    callback(new Error('请确认密码'))
  } else {
    // 和新密码输入框比对，不一致时提示
    if (value !== data.user.newPassword) {
      callback(new Error("确认密码跟原密码不一致!"))
    }
    callback()
  }
}
const data = reactive({
  // 从登录缓存拿当前用户，三个密码输入框的值也写进这个对象
  user: JSON.parse(localStorage.getItem('xm-user') || '{}'),
  rules: {
    // 原密码非空校验，失焦时触发
    password: [
      { required: true, message: '请输入原密码', trigger: 'blur' },
    ],
    // 新密码非空校验，失焦时触发
    newPassword: [
      { required: true, message: '请输入新密码', trigger: 'blur' },
    ],
    // 确认密码走上面的自定义函数
    confirmPassword: [
      { validator: validatePass, trigger: 'blur' }
    ]
  }
})

const updatePassword = () => {
  // 先跑 data.rules 里的三条校验：原密码非空、新密码非空、两次新密码一致
  formRef.value.validate(valid => {
    if (valid) {
      // 把 data.user 整体提交，后端只取 password 和 newPassword 两个字段
      request.put('/updatePassword', data.user).then(res => {
        if (res.code === '200') {
          ElMessage.success('保存成功')
          // 密码已经变了，缓存里的登录态对应的是旧密码，直接退出重登
          logout()
        } else {
          // 例如“原密码错误”“新密码不能与原密码相同”
          ElMessage.error(res.msg)
        }
      })
    }
  })
}

const logout = () => {
  // 清掉登录缓存并回到第 1 章的登录页
  localStorage.removeItem('xm-user')
  router.push('/login')
}
</script>

<style scoped>

</style>