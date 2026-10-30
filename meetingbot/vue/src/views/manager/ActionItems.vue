<template>
  <div>
    <!-- 筛选：会议 / 状态 / 负责人姓名 -->
    <div class="card" style="margin-bottom: 5px">
      <el-select v-model="data.meetingId" clearable filterable style="width: 300px; margin-right: 10px" placeholder="请选择会议">
        <el-option v-for="item in data.meetings" :key="item.id" :label="meetingLabel(item)" :value="item.id" />
      </el-select>
      <el-select v-model="data.status" clearable style="width: 150px; margin-right: 10px" placeholder="待办状态">
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <el-input v-model="data.ownerName" style="width: 180px; margin-right: 10px" placeholder="负责人姓名" @keyup.enter="search" />
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>

    <!-- 工具栏：新增 / 从纪要生成 / 批量 / 导出 -->
    <div class="card" style="margin-bottom: 5px">
      <el-button type="primary" plain @click="handleAdd">新增待办</el-button>
      <el-button type="primary" plain @click="openGenerate">从纪要生成</el-button>
      <el-button type="success" plain @click="confirmBatch">批量确认</el-button>
      <el-button type="danger" plain @click="deleteBatch">批量删除</el-button>
      <el-button plain :loading="data.exporting" style="margin-left: 10px" @click="exportCsv">导出CSV</el-button>
    </div>

    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="55" />
        <el-table-column prop="meeting_title" label="会议主题" min-width="170" show-overflow-tooltip />
        <el-table-column prop="content" label="待办内容" min-width="240" show-overflow-tooltip />
        <el-table-column label="负责人" width="130">
          <template #default="scope">{{ ownerText(scope.row) }}</template>
        </el-table-column>
        <el-table-column label="截止" width="130">
          <template #default="scope">{{ dueText(scope.row) }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="100">
          <template #default="scope">
            <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="90">
          <template #default="scope">
            <el-tag size="small" :type="sourceOf(scope.row) === 'MANUAL' ? 'info' : 'success'">
              {{ sourceOf(scope.row) === 'MANUAL' ? '手动' : 'AI' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="handleEdit(scope.row)">编辑</el-button>
            <el-button v-if="scope.row.status === 'DRAFT'" link type="success" @click="confirmOne(scope.row)">确认生效</el-button>
            <el-button v-if="scope.row.status === 'OPEN'" link type="success" @click="complete(scope.row)">完成</el-button>
            <el-button v-if="['DRAFT', 'OPEN'].includes(scope.row.status)" link type="warning" @click="cancelItem(scope.row)">撤回</el-button>
            <el-button link type="danger" @click="del(scope.row)">删除</el-button>
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

    <!-- 新增 / 编辑待办 -->
    <el-dialog :title="data.form.id ? '编辑待办' : '新增待办'" v-model="data.formVisible" width="min(92vw, 520px)" destroy-on-close>
      <el-form label-width="80px" style="padding: 0 10px">
        <el-form-item label="会议" required>
          <el-select v-model="data.form.meeting_id" filterable style="width: 100%" placeholder="请选择会议">
            <el-option v-for="item in data.meetings" :key="item.id" :label="meetingLabel(item)" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="待办内容" required>
          <el-input v-model="data.form.content" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="请输入待办内容" />
        </el-form-item>
        <el-form-item label="负责人">
          <el-select v-model="data.form.owner_user_id" clearable filterable style="width: 100%" placeholder="请选择负责人（可不选）">
            <el-option v-for="item in data.candidates" :key="item.id" :label="candidateLabel(item)" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="截止日期">
          <el-date-picker v-model="data.form.due_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" placeholder="请选择截止日期" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" :loading="data.saving" @click="save">确定</el-button>
      </template>
    </el-dialog>

    <!-- 从已确认纪要生成待办草稿 -->
    <el-dialog title="从纪要生成待办" v-model="data.generateVisible" width="min(92vw, 560px)">
      <el-alert
        type="info"
        :closable="false"
        title="选择一份已确认的纪要，系统会把纪要里的待确认事项转成行动项草稿；已经生成过的条目会自动跳过。"
        style="margin-bottom: 14px"
      />
      <el-select v-model="data.generateMinutesId" filterable style="width: 100%" placeholder="请选择已确认的纪要">
        <el-option v-for="item in data.confirmedMinutes" :key="item.id" :label="minutesLabel(item)" :value="item.id" />
      </el-select>
      <div v-if="!data.confirmedMinutes.length && data.generateLoaded" class="generate-empty">还没有已确认的纪要，请先在「AI会议纪要」页确认一份纪要。</div>
      <template #footer>
        <el-button @click="data.generateVisible = false">取消</el-button>
        <el-button type="primary" :loading="data.generating" :disabled="!data.generateMinutesId" @click="submitGenerate">生成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'

// 行动项状态：DRAFT 草稿（AI 生成待确认）、OPEN 待办、DONE 已完成、CANCELLED 已撤回
const statusOptions = [
  { label: '草稿', value: 'DRAFT' },
  { label: '待办', value: 'OPEN' },
  { label: '已完成', value: 'DONE' },
  { label: '已撤回', value: 'CANCELLED' }
]
const statusText = status => statusOptions.find(item => item.value === status)?.label || status || '-'
const statusTag = status => ({ DRAFT: 'info', OPEN: 'warning', DONE: 'success', CANCELLED: 'info' })[status] || ''
// 后端不同版本的字段名可能叫 source 或 source_field，这里兼容两种
const sourceOf = row => row?.source || row?.source_field || ''
// 负责人优先显示姓名，其次显示文本，最后用ID兜底
const ownerText = row => row?.owner_name || row?.owner_text || (row?.owner_user_id ? `#${row.owner_user_id}` : '未指定')
// 截止日期和文本日期二选一展示
const dueText = row => row?.due_date || row?.due_text || '-'
const meetingLabel = item => `${item.meeting_no} / ${item.title}`
const candidateLabel = item => [item.name, item.department_name, item.position].filter(Boolean).join(' / ')
const minutesLabel = item => [item.meeting_title, item.create_time].filter(Boolean).join(' · ') || `纪要 #${item.id}`

const data = reactive({
  // 筛选条件
  meetingId: null,
  status: '',
  ownerName: '',
  meetings: [],
  candidates: [],
  // 分页与表格
  pageNum: 1,
  pageSize: 10,
  total: 0,
  tableData: [],
  ids: [],
  // 新增/编辑弹窗
  formVisible: false,
  form: {},
  saving: false,
  // 从纪要生成弹窗
  generateVisible: false,
  generateMinutesId: null,
  confirmedMinutes: [],
  generateLoaded: false,
  generating: false,
  // 导出按钮 loading
  exporting: false
})

const loadMeetings = () =>
  request.get('/meeting/selectOptions').then(res => {
    if (res.code === '200') data.meetings = res.data || []
  }).catch(() => {})

const loadCandidates = () =>
  request.get('/user/selectMeetingCandidates').then(res => {
    if (res.code === '200') data.candidates = res.data || []
  }).catch(() => {})

const load = () => {
  const params = {
    pageNum: data.pageNum,
    pageSize: data.pageSize,
    status: data.status,
    // 负责人筛选参数名后端可能用驼峰或下划线，两个都带上，多余参数会被后端忽略
    ownerName: data.ownerName || undefined,
    owner_name: data.ownerName || undefined,
    meetingId: data.meetingId || undefined,
    meeting_id: data.meetingId || undefined
  }
  request.get('/actionItem/selectPage', { params }).then(res => {
    if (res.code === '200') {
      data.tableData = res.data?.list || []
      data.total = res.data?.total || 0
    } else ElMessage.error(res.msg || '加载行动项失败')
  }).catch(() => {})
}

const search = () => {
  data.pageNum = 1
  load()
}

const reset = () => {
  data.meetingId = null
  data.status = ''
  data.ownerName = ''
  data.pageNum = 1
  load()
}

const handleAdd = () => {
  data.form = { meeting_id: data.meetingId || null, content: '', owner_user_id: null, due_date: '' }
  data.formVisible = true
  if (!data.candidates.length) loadCandidates()
}

const handleEdit = row => {
  // 截止日期统一截成 YYYY-MM-DD，避免带时间时日期控件回显失败
  data.form = {
    id: row.id,
    meeting_id: row.meeting_id,
    content: row.content || '',
    owner_user_id: row.owner_user_id ?? null,
    due_date: row.due_date ? String(row.due_date).slice(0, 10) : ''
  }
  data.formVisible = true
  if (!data.candidates.length) loadCandidates()
}

const save = () => {
  if (!data.form.meeting_id) return ElMessage.warning('请选择会议')
  if (!data.form.content?.trim()) return ElMessage.warning('请输入待办内容')
  const payload = {
    meeting_id: data.form.meeting_id,
    content: data.form.content.trim(),
    owner_user_id: data.form.owner_user_id || null,
    due_date: data.form.due_date || null
  }
  data.saving = true
  const action = data.form.id
    ? request.put('/actionItem/update', { id: data.form.id, ...payload })
    : request.post('/actionItem/add', payload)
  action.then(res => {
    if (res.code === '200') {
      ElMessage.success('保存成功')
      data.formVisible = false
      load()
    } else ElMessage.error(res.msg || '保存失败')
  }).catch(() => {}).finally(() => {
    data.saving = false
  })
}

const handleSelectionChange = rows => {
  data.ids = rows.map(item => item.id)
}

// 单条“确认生效”复用批量确认接口，只传一个 ID
const confirmOne = row => {
  request.put('/actionItem/confirmBatch', [row.id]).then(res => {
    if (res.code === '200') {
      ElMessage.success('已确认生效')
      load()
    } else ElMessage.error(res.msg || '确认失败')
  }).catch(() => {})
}

const confirmBatch = () => {
  if (!data.ids.length) return ElMessage.warning('请选择数据')
  ElMessageBox.confirm(`确定把选中的 ${data.ids.length} 条草稿待办确认生效吗？`, '批量确认', {
    type: 'warning', confirmButtonText: '确定确认', cancelButtonText: '取消'
  }).then(() => {
    request.put('/actionItem/confirmBatch', data.ids).then(res => {
      if (res.code === '200') {
        ElMessage.success('已批量确认生效')
        load()
      } else ElMessage.error(res.msg || '批量确认失败')
    }).catch(() => {})
  }).catch(() => {})
}

const changeStatus = (row, status, successText) => {
  request.put('/actionItem/status/' + row.id, { status }).then(res => {
    if (res.code === '200') {
      ElMessage.success(successText)
      load()
    } else ElMessage.error(res.msg || '操作失败')
  }).catch(() => {})
}

const complete = row => changeStatus(row, 'DONE', '待办已完成')
const cancelItem = row => changeStatus(row, 'CANCELLED', '待办已撤回')

const del = row => {
  ElMessageBox.confirm('删除后数据无法恢复，您确定删除这条待办吗？', '删除确认', {
    type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消'
  }).then(() => {
    request.delete('/actionItem/delete/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        load()
      } else ElMessage.error(res.msg || '删除失败')
    }).catch(() => {})
  }).catch(() => {})
}

const deleteBatch = () => {
  if (!data.ids.length) return ElMessage.warning('请选择数据')
  ElMessageBox.confirm('删除后数据无法恢复，您确定删除选中的待办吗？', '批量删除', {
    type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消'
  }).then(() => {
    request.delete('/actionItem/deleteBatch', { data: data.ids }).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        data.ids = []
        load()
      } else ElMessage.error(res.msg || '批量删除失败')
    }).catch(() => {})
  }).catch(() => {})
}

const openGenerate = () => {
  data.generateVisible = true
  data.generateMinutesId = null
  if (data.generateLoaded) return
  // 只取已确认纪要，一次拉 100 条足够下拉选择
  request.get('/meetingMinutes/selectPage', {
    params: { status: 'CONFIRMED', pageNum: 1, pageSize: 100 }
  }).then(res => {
    if (res.code === '200') {
      data.confirmedMinutes = res.data?.list || []
      data.generateLoaded = true
    } else ElMessage.error(res.msg || '加载已确认纪要失败')
  }).catch(() => {})
}

const submitGenerate = () => {
  if (!data.generateMinutesId) return
  data.generating = true
  request.post('/actionItem/generate/' + data.generateMinutesId).then(res => {
    if (res.code === '200') {
      const created = res.data?.created ?? res.data?.created_count ?? 0
      const skipped = res.data?.skipped ?? res.data?.skipped_count ?? 0
      ElMessage.success(`生成完成：新增 ${created} 条${skipped ? `，跳过 ${skipped} 条` : ''}`)
      data.generateVisible = false
      load()
    } else ElMessage.error(res.msg || '生成待办失败')
  }).catch(() => {}).finally(() => {
    data.generating = false
  })
}

const exportCsv = () => {
  data.exporting = true
  request.get('/actionItem/export', {
    params: { meetingId: data.meetingId || undefined },
    responseType: 'blob'
  }).then(blob => {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    const today = new Date()
    const stamp = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, '0')}${String(today.getDate()).padStart(2, '0')}`
    link.download = `行动项-${stamp}.csv`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }).catch(() => {}).finally(() => {
    data.exporting = false
  })
}

// 首次进入：会议下拉、负责人候选、列表
loadMeetings()
loadCandidates()
load()
</script>

<style scoped>
/* 从纪要生成弹窗里没有可选纪要时的提示 */
.generate-empty {
  margin-top: 10px;
  color: #8a94a6;
  font-size: 13px;
}
</style>
