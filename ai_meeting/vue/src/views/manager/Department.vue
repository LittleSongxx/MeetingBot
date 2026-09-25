<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 部门名称输入框，输入值写入 data.name，作为模糊查询条件 -->
      <el-input v-model="data.name" style="width: 240px; margin-right: 10px" placeholder="请输入部门名称查询"></el-input>
      <!-- 状态下拉框，输入值写入 data.status；clearable 让用户能清空回到不限状态 -->
      <el-select v-model="data.status" clearable style="width: 180px; margin-right: 10px" placeholder="请选择状态">
        <el-option label="正常" value="NORMAL"></el-option>
        <el-option label="停用" value="DISABLED"></el-option>
      </el-select>
      <!-- 查询按钮直接调用 load，用当前的 data.name 和 data.status 重新请求 -->
      <el-button type="info" plain @click="load">查询</el-button>
      <!-- 重置按钮清空两个条件并回到第一页 -->
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 新增按钮，打开一张空白的部门表单 -->
      <el-button type="primary" plain @click="handleAdd">新增</el-button>
      <!-- 批量删除按钮，删除的是表格里勾选的那些行 -->
      <el-button type="danger" plain @click="delBatch">批量删除</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- selection-change 在勾选变化时把选中行交给 handleSelectionChange -->
      <el-table stripe :data="data.tableData" @selection-change="handleSelectionChange">
        <!-- 勾选列，批量删除依赖它 -->
        <el-table-column type="selection" width="55" />
        <el-table-column prop="name" label="部门名称" />
        <!-- show-overflow-tooltip 让过长的说明在一行内省略，鼠标悬停时完整显示 -->
        <el-table-column prop="description" label="部门说明" show-overflow-tooltip />
        <!-- employee_count 由后端统计 user 表得到 -->
        <el-table-column prop="employee_count" label="员工人数" width="90" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="scope">
            <!-- 把 NORMAL / DISABLED 翻译成绿色“正常”和红色“停用”标签 -->
            <el-tag :type="scope.row.status === 'NORMAL' ? 'success' : 'danger'">
              {{ scope.row.status === 'NORMAL' ? '正常' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="scope">
            <!-- 编辑图标，把当前行整行数据带进弹窗 -->
            <el-button type="primary" circle :icon="Edit" @click="handleEdit(scope.row)"></el-button>
            <!-- 删除图标，只需要当前行的ID -->
            <el-button type="danger" circle :icon="Delete" @click="del(scope.row.id)"></el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <!-- data.total 为 0 时整个分页条不渲染 -->
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

    <!-- destroy-on-close 让弹窗关闭时销毁内部组件，下次打开是干净的表单 -->
    <el-dialog title="部门信息" v-model="data.formVisible" width="42%" destroy-on-close>
      <el-form ref="formRef" :model="data.form" :rules="data.rules" label-width="90px" style="padding: 20px">
        <!-- 部门名称，必填，写入 data.form.name -->
        <el-form-item label="部门名称" prop="name">
          <el-input v-model="data.form.name" placeholder="请输入部门名称"></el-input>
        </el-form-item>
        <!-- 状态单选框，值写入 data.form.status -->
        <el-form-item label="状态">
          <el-radio-group v-model="data.form.status">
            <el-radio value="NORMAL">正常</el-radio>
            <el-radio value="DISABLED">停用</el-radio>
          </el-radio-group>
        </el-form-item>
        <!-- 部门说明文本域，值写入 data.form.description -->
        <el-form-item label="部门说明">
          <el-input v-model="data.form.description" type="textarea" :rows="4" placeholder="请输入部门说明"></el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <!-- 取消只关闭弹窗，不提交任何请求 -->
        <el-button @click="data.formVisible = false">取消</el-button>
        <!-- 确定触发 save，由 save 内部区分新增和编辑 -->
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { Delete, Edit } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'

const formRef = ref()

const data = reactive({
  // 顶部“请输入部门名称查询”输入框，作为 selectPage 的 name 参数
  name: '',
  // 顶部状态下拉框，作为 selectPage 的 status 参数
  status: '',
  // 当前页码，翻页时由 el-pagination 写入
  pageNum: 1,
  // 每页条数，随查询一起提交给后端
  pageSize: 10,
  // 总条数，控制分页条是否显示以及页码数量
  total: 0,
  // 表格数据源，保存 selectPage 返回的当前页部门
  tableData: [],
  // 表格勾选出来的部门ID集合，批量删除时提交
  ids: [],
  // 控制部门弹窗显示
  formVisible: false,
  // 弹窗表单对象，新增和编辑共用；有 id 表示编辑
  form: {},
  // 弹窗的必填校验规则，点击确定时执行
  rules: {
    name: [{ required: true, message: '请输入部门名称', trigger: 'blur' }]
  }
})


// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.tableData || data.tableData.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () => {
  // 四个查询参数拼在 params 里，Axios 会拼成 URL 查询串
  request.get('/department/selectPage', {
    params: {
      // 部门名称输入框的值，后端做模糊匹配
      name: data.name,
      // 状态下拉框的值，为空字符串时后端不加状态条件
      status: data.status,
      // 当前页码，后端据此计算跳过多少条
      page_num: data.pageNum,
      // 每页条数
      page_size: data.pageSize
    }
  }).then(res => {
    if (res.code === '200') {
      // list 是当前页数据，赋给表格数据源后表格立即重绘
      data.tableData = res.data?.list || []
      // total 是符合条件的总条数，赋值后分页条的页码数量随之变化
      data.total = res.data?.total || 0
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const handleAdd = () => {
  // 重置成一张新表单，没有 id 字段，保存时会走新增分支
  // status 预置 NORMAL，和数据库默认值保持一致
  data.form = { status: 'NORMAL' }
  data.formVisible = true
}

const handleEdit = (row) => {
  // 深拷贝当前行，弹窗里的修改不会直接影响表格上正在显示的那一行
  data.form = JSON.parse(JSON.stringify(row))
  data.formVisible = true
}

const save = () => {
  // 先跑 data.rules 里的必填校验，不通过就停在弹窗上
  formRef.value.validate(valid => {
    if (!valid) {
      return
    }
    // data.form.id 存在说明弹窗是编辑模式，后端按 id 更新原记录
    // data.form.id 不存在说明是新增模式，后端会插入一条新部门
    const action = data.form.id
        ? request.put('/department/update', data.form)
        : request.post('/department/add', data.form)
    action.then(res => {
      if (res.code === '200') {
        ElMessage.success('保存成功')
        // 关闭弹窗
        data.formVisible = false
        // 重新拉当前页列表，新增或修改的部门立即出现在表格里
        load()
      } else {
        // 例如“部门名称重复”，直接把后端提示弹出来
        ElMessage.error(res.msg)
      }
    })
  })
}

const del = (id) => {
  // 删除前先弹确认框，用户点取消时 Promise 不会进入 then，不发请求
  ElMessageBox.confirm('删除部门前需要确保没有所属员工，是否继续？', '删除确认', { type: 'warning' }).then(() => {
    // 部门ID拼在地址里，对应后端的路径参数 department_id
    request.delete('/department/delete/' + id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 重新拉列表，被删的行从表格消失
        reloadAfterDelete()
      } else {
        // 例如“请先调整部门下的员工”
        ElMessage.error(res.msg)
      }
    })
  })
}

const delBatch = () => {
  // 一条都没勾时不发请求，直接提示
  if (!data.ids.length) {
    ElMessage.warning('请选择数据')
    return
  }
  ElMessageBox.confirm('删除部门前需要确保没有所属员工，是否继续？', '删除确认', { type: 'warning' }).then(() => {
    // DELETE 请求要通过配置项的 data 字段才能带请求体，这里把勾选出的ID数组发过去
    request.delete('/department/deleteBatch', { data: data.ids }).then(res => {
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
  // el-table 把当前选中的整行对象数组传进来，这里只留下ID，供批量删除提交
  data.ids = rows.map(item => item.id)
}

const reset = () => {
  // 清空部门名称输入框
  data.name = ''
  // 清空状态下拉框
  data.status = ''
  // 回到第一页，否则清空条件后可能停在一个已经不存在的页码上
  data.pageNum = 1
  load()
}

load()
</script>
