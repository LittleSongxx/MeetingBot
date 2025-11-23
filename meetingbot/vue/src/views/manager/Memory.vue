<template>
  <div>
    <div class="card filter-card">
      <el-select v-model="data.scopeType" clearable style="width: 160px" placeholder="全部作用域">
        <el-option
          v-for="item in scopeOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-select v-model="data.kind" clearable style="width: 160px" placeholder="全部类型">
        <el-option
          v-for="item in kindOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
      <div class="filter-actions">
        <el-button type="primary" plain @click="openPreference">新增个人偏好</el-button>
        <el-tag v-if="!canManage" type="info">只读：记忆审核由管理员完成</el-tag>
      </div>
    </div>

    <div class="card">
      <el-tabs v-model="data.tab">
        <el-tab-pane :label="`待确认候选（${candidates.total}）`" name="candidates">
          <MemoryReview
            :items="candidates.list"
            :loading="candidates.loading"
            :readonly="!canManage"
            empty-text="没有待确认的记忆候选"
            @confirm="handleConfirm"
            @reject="reject"
            @edit="openEdit"
            @open-meeting="openMeeting"
          />
          <div v-if="candidates.total > candidates.pageSize" class="pager">
            <el-pagination
              v-model:current-page="candidates.pageNum"
              background
              layout="prev, pager, next"
              :page-size="candidates.pageSize"
              :total="candidates.total"
              @current-change="loadCandidates"
            />
          </div>
        </el-tab-pane>
        <el-tab-pane :label="`已确认记忆（${confirmed.total}）`" name="confirmed">
          <MemoryReview
            :items="confirmed.list"
            :loading="confirmed.loading"
            :readonly="!canManage"
            empty-text="还没有已确认的记忆"
            @expire="expire"
            @edit="openEdit"
            @open-meeting="openMeeting"
          />
          <div v-if="confirmed.total > confirmed.pageSize" class="pager">
            <el-pagination
              v-model:current-page="confirmed.pageNum"
              background
              layout="prev, pager, next"
              :page-size="confirmed.pageSize"
              :total="confirmed.total"
              @current-change="loadConfirmed"
            />
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 确认候选：可选替代一条旧记忆（冲突消解入口） -->
    <el-dialog title="确认记忆" v-model="data.confirmVisible" width="600px" destroy-on-close>
      <div class="dialog-tip">
        这条候选与下列已确认记忆相似。选择被替代的旧记忆后，新记忆生效、旧记忆标记为「已被替代」。
      </div>
      <div class="confirm-content">{{ data.confirmTarget?.content }}</div>
      <el-form label-width="100px">
        <el-form-item label="替代旧记忆">
          <el-select
            v-model="data.supersedeId"
            clearable
            filterable
            style="width: 100%"
            placeholder="请选择被替代的旧记忆"
          >
            <el-option
              v-for="item in data.replaceOptions"
              :key="item.id"
              :label="`#${item.id} ${item.content}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="dialog-hint">
        冲突不做静默合并：不选择替代目标时确认不会落库，避免同一件事出现两种说法。
      </div>
      <template #footer>
        <el-button @click="data.confirmVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!data.supersedeId" @click="submitConfirm">
          确认替代
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑记忆正文 -->
    <el-dialog title="编辑记忆" v-model="data.editVisible" width="560px" destroy-on-close>
      <el-form label-width="80px">
        <el-form-item label="内容">
          <el-input v-model="data.editContent" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">确定</el-button>
      </template>
    </el-dialog>

    <!-- 新增个人偏好（USER 级记忆，自服务） -->
    <el-dialog title="新增个人偏好" v-model="data.preferenceVisible" width="520px" destroy-on-close>
      <div class="dialog-tip">
        个人偏好只作用于你自己的问答（例如「回答先给结论」「时间用北京时间」）。
      </div>
      <el-input
        v-model="data.preferenceContent"
        type="textarea"
        :rows="3"
        placeholder="例如：回答时先给一句话结论，再展开依据"
      />
      <template #footer>
        <el-button @click="data.preferenceVisible = false">取消</el-button>
        <el-button type="primary" @click="savePreference">确定</el-button>
      </template>
    </el-dialog>

    <!-- 从引用卡片带 memoryId 进来时，直接展示那条记忆 -->
    <el-dialog title="记忆详情" v-model="data.focusVisible" width="640px">
      <MemoryReview
        :items="data.focusItems"
        readonly
        empty-text="没有找到这条记忆，可能已删除或无权查看"
        @open-meeting="openMeeting"
      />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 长期记忆页：待确认候选审核 + 已确认列表 + 个人偏好新增。
 *
 * 审核动作（确认/拒绝/编辑/过期）都要写接口并有二次确认；确认时还可以选一条
 * 同作用域同类型的已确认记忆做「替代」，这是解决同一件事多种说法的唯一入口。
 * 权限：管理员可审核，员工只读；个人偏好是自服务，员工可以给自己加。
 */
import { onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  addPreference,
  confirmMemory,
  expireMemory,
  rejectMemory,
  selectMemoryPage,
  updateMemory,
  type MeetingMemoryItem,
} from '@/api/memory'
import MemoryReview from '@/components/MemoryReview.vue'
import { MEMORY_KIND, MEMORY_SCOPE } from '@/constants/status'
import router from '@/router/index'

interface CurrentUser {
  id: number
  role: string
}

interface MemoryListState {
  list: MeetingMemoryItem[]
  total: number
  pageNum: number
  pageSize: number
  loading: boolean
}

interface MemoryPageState {
  user: CurrentUser
  tab: 'candidates' | 'confirmed'
  scopeType: string
  kind: string
  confirmVisible: boolean
  confirmTarget: MeetingMemoryItem | null
  supersedeId?: number
  replaceOptions: MeetingMemoryItem[]
  editVisible: boolean
  editTarget: MeetingMemoryItem | null
  editContent: string
  preferenceVisible: boolean
  preferenceContent: string
  focusVisible: boolean
  focusItems: MeetingMemoryItem[]
}

const user: CurrentUser = JSON.parse(localStorage.getItem('xm-user') || '{}')
const canManage = user.role === 'ADMIN'

const scopeOptions = MEMORY_SCOPE.options
const kindOptions = MEMORY_KIND.options

const candidates = reactive<MemoryListState>({
  list: [],
  total: 0,
  pageNum: 1,
  pageSize: 10,
  loading: false,
})

const confirmed = reactive<MemoryListState>({
  list: [],
  total: 0,
  pageNum: 1,
  pageSize: 10,
  loading: false,
})

const data = reactive<MemoryPageState>({
  user,
  tab: 'candidates',
  scopeType: '',
  kind: '',
  confirmVisible: false,
  confirmTarget: null,
  supersedeId: undefined,
  replaceOptions: [],
  editVisible: false,
  editTarget: null,
  editContent: '',
  preferenceVisible: false,
  preferenceContent: '',
  focusVisible: false,
  focusItems: [],
})

function loadCandidates(): void {
  candidates.loading = true
  selectMemoryPage({
    status: 'PROPOSED',
    scope_type: data.scopeType,
    kind: data.kind,
    pageNum: candidates.pageNum,
    pageSize: candidates.pageSize,
  })
    .then((res) => {
      candidates.list = res.data?.list || []
      candidates.total = res.data?.total || 0
    })
    .catch(() => {
      // 错误提示由 request 拦截器统一弹出
    })
    .finally(() => (candidates.loading = false))
}

function loadConfirmed(): void {
  confirmed.loading = true
  selectMemoryPage({
    status: 'CONFIRMED',
    scope_type: data.scopeType,
    kind: data.kind,
    pageNum: confirmed.pageNum,
    pageSize: confirmed.pageSize,
  })
    .then((res) => {
      confirmed.list = res.data?.list || []
      confirmed.total = res.data?.total || 0
    })
    .catch(() => {})
    .finally(() => (confirmed.loading = false))
}

function reload(): void {
  loadCandidates()
  loadConfirmed()
}

function search(): void {
  candidates.pageNum = 1
  confirmed.pageNum = 1
  reload()
}

function reset(): void {
  data.scopeType = ''
  data.kind = ''
  search()
}

/**
 * 点"确认"：先做一次不带替代目标的确认——没有相似活跃记忆就直接生效；
 * 有冲突时后端返回 conflicts（不落库），再弹窗让用户选择被替代的旧记忆。
 */
function handleConfirm(item: MeetingMemoryItem): void {
  confirmMemory(item.id)
    .then((res) => {
      if (res.code !== '200') {
        ElMessage.error(res.msg)
        return
      }
      const result = res.data
      if (result?.confirmed) {
        ElMessage.success('已确认')
        reload()
        return
      }
      data.confirmTarget = item
      data.replaceOptions = result?.conflicts || []
      data.supersedeId = undefined
      data.confirmVisible = true
    })
    .catch(() => {})
}

/** 带 supersede_id 再次确认：旧记忆标记 SUPERSEDED，新记忆记 supersedes_id。 */
function submitConfirm(): void {
  const target = data.confirmTarget
  const supersedeId = data.supersedeId
  if (!target || !supersedeId) return
  confirmMemory(target.id, supersedeId)
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已确认并替代旧记忆')
        data.confirmVisible = false
        reload()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function reject(item: MeetingMemoryItem): void {
  ElMessageBox.confirm('拒绝后这条候选不会进入检索，记录保留。', '拒绝候选', {
    type: 'warning',
  })
    .then(() => rejectMemory(item.id))
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已拒绝')
        loadCandidates()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function expire(item: MeetingMemoryItem): void {
  ElMessageBox.confirm('标记过期后这条记忆不再参与检索，但保留审计记录。', '标记过期', {
    type: 'warning',
  })
    .then(() => expireMemory(item.id))
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已标记过期')
        loadConfirmed()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function openEdit(item: MeetingMemoryItem): void {
  data.editTarget = item
  data.editContent = item.content
  data.editVisible = true
}

function saveEdit(): void {
  const target = data.editTarget
  if (!target) return
  const content = data.editContent.trim()
  if (!content) {
    ElMessage.warning('内容不能为空')
    return
  }
  updateMemory(target.id, content)
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已保存')
        data.editVisible = false
        reload()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function openPreference(): void {
  data.preferenceContent = ''
  data.preferenceVisible = true
}

function savePreference(): void {
  const content = data.preferenceContent.trim()
  if (!content) {
    ElMessage.warning('请输入偏好内容')
    return
  }
  addPreference(content)
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已添加个人偏好')
        data.preferenceVisible = false
        // 偏好是 USER 级、直接确认的记忆：切到已确认页并只看个人偏好
        data.scopeType = 'USER'
        data.kind = 'PREFERENCE'
        data.tab = 'confirmed'
        confirmed.pageNum = 1
        loadConfirmed()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function openMeeting(item: MeetingMemoryItem): void {
  if (!item.source_meeting_id) return
  router.push({
    path: '/manager/meeting',
    query: { meetingId: String(item.source_meeting_id) },
  })
}

/** 从 Chat 的状态引用卡片带 memoryId 进来时，展示这条记忆。 */
async function focusFromQuery(): Promise<void> {
  const raw = router.currentRoute.value.query.memoryId
  const id = Number(Array.isArray(raw) ? raw[0] : raw)
  if (!Number.isFinite(id) || id <= 0) return
  try {
    const res = await selectMemoryPage({ pageNum: 1, pageSize: 200 })
    const list = res.data?.list || []
    const target = list.find((item) => item.id === id)
    data.focusItems = target ? [target] : []
    data.focusVisible = true
  } catch {
    // 错误提示由拦截器处理
  }
}

onMounted(() => {
  reload()
  void focusFromQuery()
})
</script>

<style scoped>
.filter-card {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.filter-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.dialog-tip {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
  margin-bottom: 10px;
}

.dialog-hint {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.45);
  margin-top: 4px;
}

.confirm-content {
  border-left: 3px solid #409eff;
  background: #f8fbff;
  border-radius: 4px;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.65;
  color: rgba(17, 24, 39, 0.85);
  margin-bottom: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
