<template>
  <div>
    <div class="card filter-card">
      <el-select
        v-model="data.seriesId"
        filterable
        clearable
        style="width: 190px"
        placeholder="全部系列"
      >
        <el-option
          v-for="item in data.seriesOptions"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        />
      </el-select>
      <el-select
        v-model="data.ownerUserId"
        filterable
        clearable
        style="width: 190px"
        placeholder="全部负责人"
      >
        <el-option
          v-for="item in data.owners"
          :key="item.id"
          :label="candidateLabel(item)"
          :value="item.id"
        />
      </el-select>
      <el-select v-model="data.status" clearable style="width: 140px" placeholder="全部状态">
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-checkbox v-model="data.overdue">只看逾期</el-checkbox>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
      <div class="filter-actions">
        <el-radio-group v-model="data.viewMode">
          <el-radio-button value="kanban">看板</el-radio-button>
          <el-radio-button value="list">列表</el-radio-button>
        </el-radio-group>
        <el-button v-if="canManage" type="primary" plain @click="handleAdd">新建待办</el-button>
      </div>
    </div>

    <!-- 看板视图 -->
    <div v-if="data.viewMode === 'kanban'" class="card">
      <ActionItemBoard :items="data.tableData" :loading="data.loading" @select="openDetail" />
    </div>

    <!-- 列表视图 -->
    <div v-else class="card">
      <el-table :data="data.tableData" stripe v-loading="data.loading" empty-text="还没有待办事项">
        <el-table-column prop="content" label="待办内容" min-width="260" show-overflow-tooltip />
        <el-table-column label="系列" width="140" show-overflow-tooltip>
          <template #default="scope">{{ scope.row.series_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="来源会议" min-width="180" show-overflow-tooltip>
          <template #default="scope">
            {{ scope.row.meeting_title || `会议 ${scope.row.meeting_id}` }}
          </template>
        </el-table-column>
        <el-table-column label="负责人" width="110">
          <template #default="scope">{{ scope.row.owner_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="截止" width="170">
          <template #default="scope">
            <span v-if="isOverdue(scope.row.deadline, scope.row.status)" class="overdue">
              {{ scope.row.deadline_text || formatMinute(scope.row.deadline) }}（已逾期）
            </span>
            <span v-else>
              {{ scope.row.deadline_text || formatMinute(scope.row.deadline) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="scope">
            <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="update_time" label="更新时间" width="170" />
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="openDetail(scope.row)">详情</el-button>
            <template v-if="canManage">
              <el-button
                link
                type="primary"
                @click="openStatusDialog(scope.row)"
                :disabled="scope.row.status === 'CANCELLED'"
              >
                更新状态
              </el-button>
              <el-button link type="primary" @click="handleEdit(scope.row)">编辑</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 看板/列表共用分页 -->
    <div class="card" v-if="data.total > data.pageSize">
      <el-pagination
        v-model:current-page="data.pageNum"
        background
        layout="prev, pager, next"
        :page-size="data.pageSize"
        :total="data.total"
        @current-change="load"
      />
    </div>

    <!-- 新建 / 编辑待办 -->
    <el-dialog
      :title="data.form.id ? '编辑待办' : '新建待办'"
      v-model="data.formVisible"
      width="560px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="data.form" :rules="data.rules" label-width="90px">
        <el-form-item label="待办内容" prop="content">
          <el-input
            v-model="data.form.content"
            type="textarea"
            :rows="3"
            placeholder="要做什么、验收标准是什么"
          />
        </el-form-item>
        <!-- 来源会议与系列只在新增时选择；后端编辑接口不允许改来源 -->
        <el-form-item v-if="!data.form.id" label="来源会议" prop="meeting_id">
          <el-select
            v-model="data.form.meeting_id"
            filterable
            style="width: 100%"
            placeholder="请选择来源会议"
          >
            <el-option
              v-for="item in data.meetingOptions"
              :key="item.id"
              :label="`${item.meeting_no} / ${item.title}`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="!data.form.id" label="所属系列">
          <el-select
            v-model="data.form.series_id"
            filterable
            clearable
            style="width: 100%"
            placeholder="可留空，由后端按会议系列推导"
          >
            <el-option
              v-for="item in data.seriesOptions"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="负责人">
          <el-select
            v-model="data.form.owner_user_id"
            filterable
            clearable
            style="width: 100%"
            placeholder="从员工中选择"
            @change="changeOwner"
          >
            <el-option
              v-for="item in data.owners"
              :key="item.id"
              :label="candidateLabel(item)"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <!-- 新增只收原文期限（由后端解析成截止时间），编辑才允许直接改截止时间 -->
        <el-form-item v-if="data.form.id" label="截止时间">
          <el-date-picker
            v-model="data.form.deadline"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            style="width: 100%"
            placeholder="可留空，保留纪要里的原文期限"
          />
        </el-form-item>
        <el-form-item label="原文期限">
          <el-input v-model="data.form.deadline_text" placeholder="例如：下周五前（纪要原文）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>

    <!-- 状态更新：选择 + 备注，备注写进事件留痕 -->
    <el-dialog title="更新待办状态" v-model="data.statusVisible" width="480px" destroy-on-close>
      <div class="dialog-tip">{{ statusDialogTip }}</div>
      <el-form label-width="80px">
        <el-form-item label="新状态">
          <el-select v-model="data.statusForm.status" style="width: 100%">
            <el-option
              v-for="item in statusOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="data.statusForm.note"
            type="textarea"
            :rows="2"
            placeholder="为什么改状态（会写进变更历史）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.statusVisible = false">取消</el-button>
        <el-button type="primary" @click="saveStatus">确定</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉：来源会议/纪要/证据/变更历史 -->
    <el-drawer v-model="data.detailVisible" title="待办详情" size="600px" destroy-on-close>
      <div v-if="data.detail" class="detail-wrap">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="内容" :span="2">
            {{ data.detail.content }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTag(data.detail.status)">
              {{ statusText(data.detail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="负责人">
            {{ data.detail.owner_name || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="所属系列">
            {{ data.detail.series_name || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="来源会议">
            <el-button link type="primary" @click="openMeeting(data.detail)">
              {{ data.detail.meeting_title || `会议 ${data.detail.meeting_id}` }}
            </el-button>
          </el-descriptions-item>
          <el-descriptions-item label="截止时间">
            {{ data.detail.deadline_text || formatMinute(data.detail.deadline) }}
          </el-descriptions-item>
          <el-descriptions-item label="来源纪要">
            {{ data.detail.minutes_id ? `#${data.detail.minutes_id}` : '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="最后提及会议" :span="2">
            {{
              data.detail.last_seen_meeting_id
                ? `会议 ${data.detail.last_seen_meeting_id}`
                : '本会议'
            }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="detail-title">证据</div>
        <EvidenceList :evidence="data.detail.evidence_json" />

        <div class="detail-title">变更历史</div>
        <div v-loading="data.eventsLoading">
          <el-timeline v-if="data.events.length">
            <el-timeline-item
              v-for="event in data.events"
              :key="event.id"
              :timestamp="event.create_time"
              placement="top"
            >
              <div class="event-title">
                {{ eventTypeText(event.event_type) }}
                <template v-if="event.to_value">
                  ：{{ event.from_value || '无' }} → {{ event.to_value }}
                </template>
              </div>
              <div v-if="event.note" class="event-note">{{ event.note }}</div>
              <div class="event-actor">
                {{ event.actor_name || `用户 ${event.actor_id}` }}
              </div>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else-if="!data.eventsLoading" description="还没有变更记录" :image-size="60" />
        </div>

        <div v-if="canManage" class="drawer-actions">
          <el-button type="primary" @click="openStatusDialog(data.detail)">更新状态</el-button>
          <el-button @click="handleEdit(data.detail)">编辑</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 待办页：看板/列表两种视图 + 多维筛选 + 状态更新（HITL 留痕）+ 详情抽屉。
 *
 * 状态更新刻意走弹窗而不是看板拖拽：每次变更必须能填备注，后端把
 * 「谁、从什么状态、到什么状态、为什么」写进 action_item_event。
 * 权限：管理员可写，员工只读。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormRules } from 'element-plus'

import {
  addActionItem,
  listActionItemEvents,
  selectActionItemPage,
  updateActionItem,
  updateActionItemStatus,
  type ActionItem,
  type ActionItemEvent,
} from '@/api/actionItem'
import { seriesOptions, type SeriesOption } from '@/api/series'
import ActionItemBoard from '@/components/ActionItemBoard.vue'
import EvidenceList from '@/components/EvidenceList.vue'
import { ACTION_ITEM_STATUS } from '@/constants/status'
import { formatMinute, isOverdue } from '@/utils/format'
import request from '@/utils/request'
import router from '@/router/index'

interface CurrentUser {
  id: number
  role: string
}

interface OwnerCandidate {
  id: number
  name: string
  department_name: string | null
  position: string | null
}

interface MeetingOption {
  id: number
  meeting_no: string
  title: string
}

interface ActionItemForm {
  id?: number
  content: string
  meeting_id?: number
  series_id?: number
  owner_user_id?: number
  owner_name?: string | null
  deadline?: string
  deadline_text?: string
}

interface StatusForm {
  id?: number
  status: string
  note: string
}

interface ActionItemPageState {
  user: CurrentUser
  viewMode: 'kanban' | 'list'
  seriesId?: number
  ownerUserId?: number
  status: string
  overdue: boolean
  pageNum: number
  pageSize: number
  total: number
  tableData: ActionItem[]
  loading: boolean
  seriesOptions: SeriesOption[]
  owners: OwnerCandidate[]
  meetingOptions: MeetingOption[]
  formVisible: boolean
  form: ActionItemForm
  rules: FormRules
  statusVisible: boolean
  statusForm: StatusForm
  detailVisible: boolean
  detail: ActionItem | null
  events: ActionItemEvent[]
  eventsLoading: boolean
}

const formRef = ref()
const user: CurrentUser = JSON.parse(localStorage.getItem('xm-user') || '{}')
const canManage = user.role === 'ADMIN'

const statusOptions = ACTION_ITEM_STATUS.options
const statusText = ACTION_ITEM_STATUS.text

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
const statusTag = ACTION_ITEM_STATUS.tag as (status?: string) => TagType

const EVENT_TYPE_TEXT: Record<string, string> = {
  CREATE: '创建',
  STATUS: '状态变更',
  OWNER: '负责人变更',
  DEADLINE: '截止时间变更',
  CARRY_OVER: '跨会议结转',
}

const eventTypeText = (type?: string) => (type && EVENT_TYPE_TEXT[type]) || type || ''

const data = reactive<ActionItemPageState>({
  user,
  viewMode: 'kanban',
  seriesId: undefined,
  ownerUserId: undefined,
  status: '',
  overdue: false,
  pageNum: 1,
  pageSize: 10,
  total: 0,
  tableData: [],
  loading: false,
  seriesOptions: [],
  owners: [],
  meetingOptions: [],
  formVisible: false,
  form: { content: '' },
  rules: {
    content: [{ required: true, message: '请输入待办内容', trigger: 'blur' }],
    meeting_id: [{ required: true, message: '请选择来源会议', trigger: 'change' }],
  },
  statusVisible: false,
  statusForm: { status: 'OPEN', note: '' },
  detailVisible: false,
  detail: null,
  events: [],
  eventsLoading: false,
})

const candidateLabel = (item: OwnerCandidate) =>
  [item.name, item.department_name, item.position].filter(Boolean).join(' / ')

const statusDialogTip = computed(() => {
  const current = data.tableData.find((item) => item.id === data.statusForm.id)
  return current
    ? `把「${current.content}」从「${statusText(current.status)}」改为「${statusText(
        data.statusForm.status,
      )}」，备注会写进变更历史。`
    : '备注会写进变更历史。'
})

function load(): void {
  data.loading = true
  selectActionItemPage({
    series_id: data.seriesId,
    owner_user_id: data.ownerUserId,
    status: data.status,
    overdue: data.overdue,
    pageNum: data.pageNum,
    pageSize: data.pageSize,
  })
    .then((res) => {
      data.tableData = res.data?.list || []
      data.total = res.data?.total || 0
    })
    .catch(() => {
      // 错误提示由 request 拦截器统一弹出
    })
    .finally(() => (data.loading = false))
}

function search(): void {
  data.pageNum = 1
  load()
}

function reset(): void {
  data.seriesId = undefined
  data.ownerUserId = undefined
  data.status = ''
  data.overdue = false
  data.pageNum = 1
  load()
}

function loadOptions(): void {
  seriesOptions()
    .then((res) => {
      data.seriesOptions = res.data || []
    })
    .catch(() => {})
  requestOptions()
}

/** 负责人候选与会议下拉复用现有人员/会议选项接口。 */
function requestOptions(): void {
  request
    .get<OwnerCandidate[]>('/user/selectMeetingCandidates')
    .then((res) => {
      data.owners = res.data || []
    })
    .catch(() => {})
  request
    .get<MeetingOption[]>('/meeting/selectOptions')
    .then((res) => {
      data.meetingOptions = res.data || []
    })
    .catch(() => {})
}

function handleAdd(): void {
  data.form = { content: '' }
  data.formVisible = true
}

function handleEdit(raw: unknown): void {
  // el-table 行插槽给的是 Record<PropertyKey, any>；在函数入口收窄成 ActionItem
  const row = raw as ActionItem
  data.form = {
    id: row.id,
    content: row.content,
    meeting_id: row.meeting_id,
    series_id: row.series_id ?? undefined,
    owner_user_id: row.owner_user_id ?? undefined,
    owner_name: row.owner_name,
    deadline: row.deadline || undefined,
    deadline_text: row.deadline_text || '',
  }
  data.formVisible = true
  data.detailVisible = false
}

/** 选中负责人时同步 owner_name：未落用户的历史数据仍有可读姓名 */
function changeOwner(value?: number): void {
  const candidate = data.owners.find((item) => item.id === value)
  data.form.owner_name = candidate?.name || null
}

function save(): void {
  formRef.value.validate((valid: boolean) => {
    if (!valid) return
    // 新增与编辑的可写字段不同：新增能带来源会议/系列，编辑只能改内容/负责人/期限
    const action = data.form.id
      ? updateActionItem({
          id: data.form.id,
          content: data.form.content,
          owner_user_id: data.form.owner_user_id ?? null,
          deadline: data.form.deadline || null,
          deadline_text: data.form.deadline_text || null,
        })
      : addActionItem({
          meeting_id: Number(data.form.meeting_id),
          content: data.form.content,
          series_id: typeof data.form.series_id === 'number' ? data.form.series_id : null,
          owner_user_id: data.form.owner_user_id ?? null,
          owner_name: data.form.owner_name || null,
          deadline_text: data.form.deadline_text || null,
        })
    action
      .then((res) => {
        if (res.code === '200') {
          ElMessage.success('保存成功')
          data.formVisible = false
          load()
        } else {
          ElMessage.error(res.msg)
        }
      })
      .catch(() => {})
  })
}

function openStatusDialog(raw: unknown): void {
  const row = raw as ActionItem
  data.statusForm = {
    id: row.id,
    // 默认选下一个状态：未完成 → DONE，完成/取消 → OPEN，减少一次选择
    status: row.status === 'OPEN' || row.status === 'BLOCKED' ? 'DONE' : 'OPEN',
    note: '',
  }
  data.statusVisible = true
  if (!data.seriesOptions.length) loadOptions()
}

function saveStatus(): void {
  const id = data.statusForm.id
  if (!id) return
  const current = data.tableData.find((item) => item.id === id)
  if (current && current.status === data.statusForm.status) {
    ElMessage.warning('状态没有变化，无需更新')
    return
  }
  updateActionItemStatus({ id, status: data.statusForm.status, note: data.statusForm.note })
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('状态已更新')
        data.statusVisible = false
        load()
        if (data.detail && data.detail.id === id) data.detail.status = data.statusForm.status
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function openDetail(raw: unknown): void {
  const row = raw as ActionItem
  data.detail = row
  data.detailVisible = true
  data.events = []
  data.eventsLoading = true
  listActionItemEvents(row.id)
    .then((res) => {
      data.events = res.data || []
    })
    .catch(() => {
      data.events = []
    })
    .finally(() => (data.eventsLoading = false))
}

function openMeeting(item: ActionItem | null): void {
  if (!item) return
  router.push({ path: '/manager/meeting', query: { meetingId: String(item.meeting_id) } })
}

/** 从别的页面带着 actionItemId 进来时，找到这条并直接打开详情。 */
async function openFromQuery(): Promise<void> {
  const raw = router.currentRoute.value.query.actionItemId
  const id = Number(Array.isArray(raw) ? raw[0] : raw)
  if (!Number.isFinite(id) || id <= 0) return
  const hit = data.tableData.find((item) => item.id === id)
  if (hit) {
    openDetail(hit)
    return
  }
  // 不在当前筛选结果里：取一页较大的数据把它捞出来（待办没有单独的 selectById 接口）
  try {
    const res = await selectActionItemPage({ pageNum: 1, pageSize: 200 })
    const target = (res.data?.list || []).find((item) => item.id === id)
    if (target) openDetail(target)
    else ElMessage.warning('没有找到这条待办，可能已删除或无权查看')
  } catch {
    // 错误提示由拦截器处理
  }
}

onMounted(() => {
  loadOptions()
  load()
  void openFromQuery()
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

.overdue {
  color: #f56c6c;
  font-weight: 600;
}

.dialog-tip {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
  margin-bottom: 12px;
}

.detail-title {
  font-weight: 600;
  font-size: 14px;
  color: rgba(17, 24, 39, 0.85);
  margin: 18px 0 8px;
}

.event-title {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.85);
}

.event-note {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.65);
  margin-top: 2px;
}

.event-actor {
  font-size: 12px;
  color: rgba(17, 24, 39, 0.4);
  margin-top: 2px;
}

.drawer-actions {
  display: flex;
  gap: 10px;
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid #f0f2f5;
}
</style>
