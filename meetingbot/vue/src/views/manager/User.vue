<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 姓名输入框，写入 data.name，后端按姓名模糊匹配 -->
      <el-input v-model="data.name" style="width: 240px; margin-right: 10px" placeholder="请输入姓名查询"></el-input>
      <!-- 角色下拉框，写入 data.role -->
      <el-select v-model="data.role" clearable style="width: 180px; margin-right: 10px" placeholder="请选择角色">
        <el-option label="管理员" value="ADMIN"></el-option>
        <el-option label="员工" value="EMPLOYEE"></el-option>
      </el-select>
      <!-- 部门下拉框，选项来自 loadDepartments 拉回的部门列表，写入 data.departmentId -->
      <el-select v-model="data.departmentId" clearable style="width: 200px; margin-right: 10px" placeholder="请选择部门">
        <el-option v-for="item in data.departments" :key="item.id" :label="item.name" :value="item.id"></el-option>
      </el-select>
      <el-button type="info" plain @click="load">查询</el-button>
      <el-button type="warning" plain style="margin: 0 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-button type="primary" plain @click="handleAdd">新增</el-button>
      <el-button type="danger" plain @click="delBatch">批量删除</el-button>
    </div>

    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="55" />
        <el-table-column prop="username" label="账号" />
        <el-table-column prop="avatar" label="头像" width="80">
          <template #default="scope">
            <!-- 没有上传头像的用户这一格留空 -->
            <el-image
                v-if="scope.row.avatar"
                style="width: 40px; height: 40px; border-radius: 50%; display: block"
                :src="fileUrl(scope.row.avatar)"
                :preview-src-list="[fileUrl(scope.row.avatar)]"
                preview-teleported
            ></el-image>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="姓名" />
        <el-table-column prop="role" label="角色">
          <template #default="scope">
            <!-- 把数据库里的 ADMIN / EMPLOYEE 翻译成中文 -->
            {{ scope.row.role === 'ADMIN' ? '管理员' : '员工' }}
          </template>
        </el-table-column>
        <el-table-column prop="phone" label="电话" />
        <el-table-column prop="email" label="邮箱" />
        <!-- department_name 由后端查部门记录后补上 -->
        <el-table-column prop="department_name" label="所属部门" />
        <el-table-column prop="position" label="职位" />
        <el-table-column prop="status" label="状态">
          <template #default="scope">
            <!-- 停用账号显示红色标签，第 1 章的登录接口会拒绝这些账号登录 -->
            <el-tag :type="scope.row.status === 'NORMAL' ? 'success' : 'danger'">
              {{ scope.row.status === 'NORMAL' ? '正常' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="scope">
            <el-button type="primary" circle :icon="Edit" @click="handleEdit(scope.row)"></el-button>
            <el-button type="danger" circle :icon="Delete" @click="del(scope.row.id)"></el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="card" v-if="data.total">
      <el-pagination
          v-model:current-page="data.pageNum"
          background
          layout="prev, pager, next"
          :page-size="data.pageSize"
          :total="data.total"
          @current-change="load"
      />
    </div>

    <el-dialog title="用户信息" v-model="data.formVisible" width="40%" destroy-on-close>
      <el-form :model="data.form" label-width="70px" style="padding: 20px">
        <!-- 账号输入框。编辑模式下 data.form.id 有值，输入框被禁用，账号不能改 -->
        <el-form-item label="账号">
          <el-input v-model="data.form.username" placeholder="请输入账号" :disabled="data.form.id !== undefined"></el-input>
        </el-form-item>
        <!-- 密码输入框只在新增模式出现；编辑时不显示，也就不会提交密码 -->
        <el-form-item v-if="!data.form.id" label="密码">
          <el-input v-model="data.form.password" show-password placeholder="不填写时默认密码为123456"></el-input>
        </el-form-item>
        <!-- 头像上传。el-upload 不走 Axios 实例，所以要自己把登录 Token 放进 headers -->
        <!-- on-success 在上传完成后把返回的地址写进表单，on-error 在网络失败时提示 -->
        <el-form-item label="头像">
          <el-upload
              :action="baseUrl + '/files/upload'"
              :headers="{ token: data.user.token || '' }"
              :on-success="handleFileUpload"
              :on-error="handleFileUploadError"
              list-type="picture"
          >
            <el-button type="primary">点击上传</el-button>
          </el-upload>
        </el-form-item>
        <!-- 姓名输入框，写入 data.form.name -->
        <el-form-item label="姓名">
          <el-input v-model="data.form.name" placeholder="请输入姓名"></el-input>
        </el-form-item>
        <!-- 角色下拉框。选 ADMIN 就把这个账号提升为管理员 -->
        <el-form-item label="角色">
          <el-select v-model="data.form.role" style="width: 100%">
            <el-option label="管理员" value="ADMIN"></el-option>
            <el-option label="员工" value="EMPLOYEE"></el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="电话">
          <el-input v-model="data.form.phone" placeholder="请输入电话"></el-input>
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="data.form.email" placeholder="请输入邮箱"></el-input>
        </el-form-item>
        <!-- 所属部门下拉框，选项来自 loadDepartments；这里选的部门写进 user.department_id -->
        <el-form-item label="所属部门">
          <el-select v-model="data.form.department_id" clearable style="width: 100%" placeholder="请选择部门">
            <el-option v-for="item in data.departments" :key="item.id" :label="item.name" :value="item.id"></el-option>
          </el-select>
        </el-form-item>
        <!-- 职位输入框，写进 user.position -->
        <el-form-item label="职位">
          <el-input v-model="data.form.position" placeholder="请输入职位"></el-input>
        </el-form-item>
        <!-- 状态单选框。切成 DISABLED 后这个账号无法登录 -->
        <el-form-item label="状态">
          <el-radio-group v-model="data.form.status">
            <el-radio value="NORMAL">正常</el-radio>
            <el-radio value="DISABLED">停用</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { Delete, Edit } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'
import { fileUrl } from '@/utils/fileUrl.js'

const baseUrl = import.meta.env.VITE_BASE_URL

const data = reactive({
  // 从第 1 章登录时写入的 xm-user 缓存里读当前登录用户，头像上传要用里面的 token
  user: JSON.parse(localStorage.getItem('xm-user') || '{}'),
  // 控制用户弹窗显示
  formVisible: false,
  // 弹窗表单对象，新增和编辑共用；有 id 表示编辑
  form: {},
  // 表格数据源，保存 selectPage 返回的当前页用户
  tableData: [],
  // 当前页码
  pageNum: 1,
  // 每页条数
  pageSize: 10,
  // 总条数
  total: 0,
  // 顶部姓名输入框
  name: '',
  // 顶部角色下拉框
  role: '',
  // 顶部部门下拉框，默认 null 表示不限部门
  departmentId: null,
  // 顶部和弹窗共用的部门下拉数据源
  departments: [],
  // 表格勾选出来的用户ID集合
  ids: []
})

const loadDepartments = () => {
  // 顶部部门筛选和弹窗“所属部门”下拉共用这一份数据
  request.get('/department/selectAll').then(res => {
    if (res.code === '200') {
      data.departments = res.data || []
    }
  })
}


// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.tableData || data.tableData.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () => {
  request.get('/user/selectPage', {
    params: {
      // 当前页码
      page_num: data.pageNum,
      // 每页条数
      page_size: data.pageSize,
      // 姓名输入框的值
      name: data.name,
      // 角色下拉框的值
      role: data.role,
      // 部门下拉框的值，为 null 时后端不加部门条件
      department_id: data.departmentId
    }
  }).then(res => {
    if (res.code === '200') {
      // 当前页用户写入表格数据源
      data.tableData = res.data?.list || []
      // 总条数写入分页条
      data.total = res.data?.total || 0
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const handleAdd = () => {
  // 新表单没有 id，保存时走新增分支
  // 角色默认员工、状态默认正常，与数据库默认值一致
  data.form = { role: 'EMPLOYEE', status: 'NORMAL' }
  data.formVisible = true
}

const handleEdit = (row) => {
  // 深拷贝当前行，弹窗里的改动不会直接影响表格上正在显示的那一行
  data.form = JSON.parse(JSON.stringify(row))
  data.formVisible = true
}

const save = () => {
  // 有 id 走编辑，没有 id 走新增
  const action = data.form.id
      ? request.put('/user/update', data.form)
      : request.post('/user/add', data.form)
  action.then(res => {
    if (res.code === '200') {
      ElMessage.success('保存成功')
      data.formVisible = false
      // 重新拉当前页，新增或修改的账号立即出现在表格里
      load()
    } else {
      // 例如“账号重复”“所选部门不存在或已停用”
      ElMessage.error(res.msg)
    }
  })
}

const del = (id) => {
  ElMessageBox.confirm('删除后数据无法恢复，您确定删除吗？', '删除确认', { type: 'warning' }).then(() => {
    // 用户ID拼在地址里，对应后端的路径参数 user_id
    request.delete('/user/delete/' + id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        reloadAfterDelete()
      } else {
        // 例如“不能删除当前登录账号”“该用户已关联会议、转写、纪要或AI运行数据，不能删除”
        ElMessage.error(res.msg)
      }
    })
  })
}

const delBatch = () => {
  // 一条都没勾时直接提示，不发请求
  if (!data.ids.length) {
    ElMessage.warning('请选择数据')
    return
  }
  ElMessageBox.confirm('删除后数据无法恢复，您确定删除吗？', '删除确认', { type: 'warning' }).then(() => {
    // DELETE 请求通过配置项的 data 字段带上勾选的用户ID数组
    request.delete('/user/deleteBatch', { data: data.ids }).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        reloadAfterDelete()
      } else {
        ElMessage.error(res.msg)
      }
    })
  })
}

const handleSelectionChange = (rows) => {
  // 勾选变化时保留选中行的ID，供批量删除提交
  data.ids = rows.map(item => item.id)
}

const handleFileUpload = (res) => {
  // res 是后端返回的统一结构，只有 code 为 '200' 时 res.data 才是文件访问地址
  if (res.code === '200') {
    // 写进表单后，点击“确定”时随 user 一起提交，最终存进 user.avatar
    data.form.avatar = res.data
  } else {
    // 上传被后端拒绝时（例如格式不支持、超过5MB）把提示展示出来
    ElMessage.error(res.msg || '头像上传失败')
  }
}

const handleFileUploadError = () => {
  // 请求本身失败时（例如后端未启动）由这里提示，避免用户以为已经传上去了
  ElMessage.error('头像上传失败')
}

const reset = () => {
  // 清空姓名输入框
  data.name = ''
  // 清空角色下拉框
  data.role = ''
  // 清空部门下拉框，恢复成不限部门
  data.departmentId = null
  // 回到第一页
  data.pageNum = 1
  load()
}

loadDepartments()
load()
</script>
