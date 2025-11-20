<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 部门名称直接取自登录缓存里的 department_name，没有部门时显示占位文案 -->
      <div style="font-size: 18px; font-weight: bold">{{ data.user.department_name || '暂未分配部门' }}</div>
      <div style="margin-top: 8px; color: #666">当前页面展示与您属于同一部门的在职员工。</div>
    </div>
    <div class="card">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="avatar" label="头像" width="80">
          <template #default="scope">
            <!-- el-avatar 在 src 为空时显示默认灰色头像，不需要额外判空 -->
            <el-avatar :size="40" :src="scope.row.avatar"></el-avatar>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="姓名" />
        <!-- position 由管理员在员工账号管理页填写 -->
        <el-table-column prop="position" label="职位" />
        <el-table-column prop="phone" label="电话" />
        <el-table-column prop="email" label="邮箱" />
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import request from '@/utils/request.js'

const data = reactive({
  // 从第 1 章登录时写入的 xm-user 缓存里读当前登录用户，用于显示部门名称
  user: JSON.parse(localStorage.getItem('xm-user') || '{}'),
  // 同部门在职员工列表
  tableData: []
})

const load = () => {
  // 接口不需要传部门参数，后端直接用 Token 里的当前用户的部门
  request.get('/user/selectDepartmentMembers').then(res => {
    if (res.code === '200') {
      // 返回的成员列表写入表格数据源
      data.tableData = res.data || []
    } else {
      ElMessage.error(res.msg)
    }
  })
}

load()
</script>
