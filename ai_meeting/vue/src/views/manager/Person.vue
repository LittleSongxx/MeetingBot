<template>
  <div style="width: 50%" class="card">
    <el-form ref="user" :model="data.user" label-width="70px" style="padding: 20px">
      <el-form-item prop="avatar" label="头像">
        <el-upload
            :action="baseUrl + '/files/upload'"
            :headers="{ token: data.user.token || '' }"
            :on-success="handleFileUpload"
            :on-error="handleFileUploadError"
            :show-file-list="false"
            class="avatar-uploader"
        >
          <!-- 已有头像时把它当成点击区域，点图片即可重新选择 -->
          <img v-if="data.user.avatar" :src="fileUrl(data.user.avatar)" class="avatar" />
          <!-- 没有头像时显示一个加号占位框 -->
          <el-icon v-else class="avatar-uploader-icon"><Plus /></el-icon>
        </el-upload>
      </el-form-item>
      <!-- 账号只读。第 2 章的 update 接口把 username 排除在更新字段之外，页面这里也禁用 -->
      <el-form-item prop="username" label="用户名">
        <el-input disabled v-model="data.user.username" placeholder="请输入用户名"></el-input>
      </el-form-item>
      <!-- 姓名可改，写入 data.user.name -->
      <el-form-item prop="name" label="姓名">
        <el-input v-model="data.user.name" placeholder="请输入姓名"></el-input>
      </el-form-item>
      <!-- 电话可改，写入 data.user.phone -->
      <el-form-item prop="phone" label="电话">
        <el-input v-model="data.user.phone" placeholder="请输入电话"></el-input>
      </el-form-item>
      <!-- 邮箱可改，写入 data.user.email -->
      <el-form-item prop="email" label="邮箱">
        <el-input v-model="data.user.email" placeholder="请输入邮箱"></el-input>
      </el-form-item>
      <!-- 所属部门只读，值来自登录时后端查部门表补上的 department_name -->
      <el-form-item label="所属部门">
        <el-input v-model="data.user.department_name" disabled placeholder="暂未分配部门"></el-input>
      </el-form-item>
      <!-- 职位只读，由第 2 章管理员在员工账号管理页维护 -->
      <el-form-item label="职位">
        <el-input v-model="data.user.position" disabled placeholder="暂未设置职位"></el-input>
      </el-form-item>
      <div style="text-align: center">
        <!-- 保存按钮，把整个 data.user 提交给 /user/update -->
        <el-button type="primary" @click="update">保 存</el-button>
      </div>
    </el-form>
  </div>
</template>

<script setup>
import { reactive } from "vue";
import request from "@/utils/request.js";
import { fileUrl } from "@/utils/fileUrl.js";
import {ElMessage} from "element-plus";

const baseUrl = import.meta.env.VITE_BASE_URL

const data = reactive({
  // 直接把登录缓存整份拿来当表单模型，页面上的输入框改的就是这份对象
  user: JSON.parse(localStorage.getItem('xm-user') || '{}')
})

const handleFileUpload = (res) => {
  // res 是后端的统一返回结构，只有 code 为 '200' 时 res.data 才是文件访问地址
  if (res.code === '200') {
    // 写进 data.user.avatar 后，上面的 img 立即换成新头像
    // 此时还没有落库，要点“保 存”才会写进 user 表
    data.user.avatar = res.data
  } else {
    // 例如格式不支持、超过5MB、登录失效，把后端提示展示出来
    ElMessage.error(res.msg || '头像上传失败')
  }
}

const handleFileUploadError = () => {
  // 请求本身失败时（例如后端未启动）由这里提示
  ElMessage.error('头像上传失败')
}

const emit = defineEmits(['updateUser'])
const update = () => {
  // 把整份 data.user 提交给第 2 章的更新接口，其中带着 id，后端按它定位记录
  request.put('/user/update', data.user).then(res => {
    if (res.code === '200') {
      ElMessage.success('保存成功')
      // 后端只回 code，不回数据，所以直接把页面上这份对象写回缓存
      localStorage.setItem('xm-user', JSON.stringify(data.user))
      // 通知框架页重新读缓存，顶部头像和姓名立即刷新
      emit('updateUser')
    } else {
      ElMessage.error(res.msg)
    }
  })
}
</script>

<style scoped>
.avatar-uploader {
  height: 120px;
}
.avatar-uploader .avatar {
  width: 120px;
  height: 120px;
  display: block;
}
.avatar-uploader .el-upload {
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  cursor: pointer;
  position: relative;
  overflow: hidden;
  transition: var(--el-transition-duration-fast);
}

.avatar-uploader .el-upload:hover {
  border-color: var(--el-color-primary);
}

.el-icon.avatar-uploader-icon {
  font-size: 28px;
  color: rgba(17, 24, 39, .55);
  width: 120px;
  height: 120px;
  text-align: center;
}
</style>
