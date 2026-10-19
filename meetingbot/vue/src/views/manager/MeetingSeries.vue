<template>
  <div>
    <div class="card filter-card">
      <el-input
        v-model="data.name"
        style="width: 220px"
        placeholder="请输入系列名称"
        clearable
        @keyup.enter="search"
      />
      <el-select v-model="data.status" clearable style="width: 150px" placeholder="全部状态">
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-select v-model="data.cadence" clearable style="width: 150px" placeholder="全部周期">
        <el-option
          v-for="item in cadenceOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
      <div class="filter-actions">
        <el-button v-if="canManage" type="primary" plain @click="handleAdd">新建系列</el-button>
        <el-button v-if="canManage" plain @click="openSuggest">建议系列</el-button>
      </div>
    </div>

    <div class="card">
      <el-table :data="data.tableData" stripe v-loading="data.loading" empty-text="还没有会议系列">
        <el-table-column prop="name" label="系列名称" min-width="180">
          <template #default="scope">
            <el-button link type="primary" @click="openDetail(scope.row.id)">
              {{ scope.row.name }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="周期" width="100">
          <template #default="scope">{{ cadenceText(scope.row.cadence) }}</template>
        </el-table-column>
        <el-table-column prop="owner_name" label="负责人" width="110">
          <template #default="scope">{{ scope.row.owner_name || '-' }}</template>
        </el-table-column>
        <el-table-column prop="meeting_count" label="会议数" width="90" />
        <el-table-column label="未完成待办" width="110">
          <template #default="scope">
            <el-tag v-if="scope.row.open_action_count" type="warning">
              {{ scope.row.open_action_count }}
            </el-tag>
            <span v-else>0</span>
          </template>
        </el-table-column>
        <el-table-column prop="memory_count" label="记忆数" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="scope">
            <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="创建时间" width="170" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="openDetail(scope.row.id)">详情</el-button>
            <el-button
              v-if="canManageRow(scope.row)"
              link
              type="primary"
              @click="handleEdit(scope.row)"
            >
              编辑
            </el-button>
            <el-button
              v-if="canManageRow(scope.row) && scope.row.status === 'ACTIVE'"
              link
              type="danger"
              @click="archive(scope.row)"
            >
              归档
            </el-button>
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

    <!-- 新建 / 编辑系列 -->
    <el-dialog
      :title="data.form.id ? '编辑系列' : '新建系列'"
      v-model="data.formVisible"
      width="520px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="data.form" :rules="data.rules" label-width="90px">
        <el-form-item label="系列名称" prop="name">
          <el-input v-model="data.form.name" placeholder="例如：产品发布周会" />
        </el-form-item>
        <el-form-item label="周期" prop="cadence">
          <el-select v-model="data.form.cadence" style="width: 100%">
            <el-option
              v-for="item in cadenceOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="data.form.description"
            type="textarea"
            :rows="3"
            placeholder="这个系列讨论什么、多久开一次"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>

    <!-- 建议系列：选一场会议，看候选与理由，可一键归入 -->
    <el-dialog title="建议系列" v-model="data.suggestVisible" width="640px" destroy-on-close>
      <div class="dialog-tip">
        选择一场会议，系统会按各系列的历史会议内容推荐候选系列，并给出可核对的理由。
        归入系列会写回会议本身，因此只有仍可编辑（待开始）的会议能改。
      </div>
      <el-select
        v-model="data.suggestMeetingId"
        filterable
        clearable
        style="width: 100%"
        placeholder="请选择会议"
      >
        <el-option
          v-for="item in data.meetingOptions"
          :key="item.id"
          :label="`${item.meeting_no} / ${item.title}`"
          :value="item.id"
        />
      </el-select>
      <div class="suggest-actions">
        <el-button
          type="primary"
          :loading="data.suggestLoading"
          :disabled="!data.suggestMeetingId"
          @click="loadSuggestions"
        >
          推荐
        </el-button>
      </div>
      <div v-if="data.suggestions.length" class="suggest-list">
        <div v-for="candidate in data.suggestions" :key="candidate.series_id" class="suggest-item">
          <div class="suggest-head">
            <span class="suggest-name">{{ candidate.name }}</span>
            <span class="suggest-score">相似度 {{ formatPercent(candidate.score) }}</span>
            <el-button link type="primary" @click="applySuggestion(candidate)"
              >归入该系列</el-button
            >
          </div>
          <ul v-if="candidate.reasons?.length" class="suggest-reasons">
            <li v-for="(reason, index) in candidate.reasons" :key="index">{{ reason }}</li>
          </ul>
        </div>
      </div>
      <el-empty v-else-if="data.suggested" description="没有找到合适的候选系列" :image-size="60" />
    </el-dialog>

    <!-- 系列详情：时间线 / 记忆 / 待办看板 / 待确认候选 -->
    <el-drawer v-model="data.detailVisible" :title="detailTitle" size="72%" destroy-on-close>
      <div v-loading="data.detailLoading" class="detail-wrap">
        <template v-if="data.detail">
          <el-descriptions :column="3" border>
            <el-descriptions-item label="周期">
              {{ cadenceText(data.detail.cadence) }}
            </el-descriptions-item>
            <el-descriptions-item label="负责人">
              {{ data.detail.owner_name || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag :type="statusTag(data.detail.status)">
                {{ statusText(data.detail.status) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="会议数">
              {{ data.detail.meeting_count ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="未完成待办">
              {{ data.detail.open_action_count ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="记忆数">
              {{ data.detail.memory_count ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="待确认记忆">
              {{ data.detail.pending_memory_count ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="描述" :span="3">
              {{ data.detail.description || '-' }}
            </el-descriptions-item>
          </el-descriptions>

          <el-tabs v-model="data.detailTab" class="detail-tabs">
            <el-tab-pane label="会议时间线" name="meetings">
              <SeriesTimeline
                :meetings="data.detail.meetings || []"
                :loading="data.detailLoading"
                @open="openMeeting"
              />
            </el-tab-pane>
            <el-tab-pane :label="`决策记忆（${data.detailMemories.length}）`" name="memories">
              <MemoryReview
                :items="data.detailMemories"
                :loading="data.detailLoading"
                readonly
                empty-text="这个系列还没有已确认的决策记忆"
                @open-meeting="openMeetingById"
              />
            </el-tab-pane>
            <el-tab-pane :label="`待办看板（${data.detailActionItems.length}）`" name="actions">
              <ActionItemBoard
                :items="data.detailActionItems"
                :loading="data.detailLoading"
                @select="openActionItem"
              />
            </el-tab-pane>
            <el-tab-pane :label="`待确认记忆（${data.detailCandidates.length}）`" name="candidates">
              <MemoryReview
                :items="data.detailCandidates"
                :loading="data.detailLoading"
                readonly
                empty-text="没有待确认的记忆候选"
                @open-meeting="openMeetingById"
              />
            </el-tab-pane>
          </el-tabs>
        </template>
        <el-empty v-else-if="!data.detailLoading" description="系列详情加载失败或系列已被删除" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 会议系列页：周会/项目例会的容器。
 *
 * 三块职责：列表（搜索/分页/新建/编辑/归档）、建议系列（为一场会议推荐候选
 * 并给出理由）、详情抽屉（会议时间线 + 决策记忆 + 待办看板 + 待确认候选）。
 * 权限：管理员可写，员工只读（按钮不渲染，后端仍会独立校验）。
 */
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormRules } from 'element-plus'

import {
  addSeries,
  archiveSeries,
  assignMeetingToSeries,
  getSeriesDetail,
  selectSeriesPage,
  suggestSeries,
  updateSeries,
  type MeetingSeriesDetail,
  type MeetingSeriesItem,
  type SeriesSuggestionCandidate,
} from '@/api/series'
import { selectMemoryPage, type MeetingMemoryItem } from '@/api/memory'
import { selectActionItemPage, type ActionItem } from '@/api/actionItem'
import ActionItemBoard from '@/components/ActionItemBoard.vue'
import MemoryReview from '@/components/MemoryReview.vue'
import SeriesTimeline from '@/components/SeriesTimeline.vue'
import { SERIES_CADENCE, SERIES_STATUS } from '@/constants/status'
import { formatPercent } from '@/utils/format'
import router from '@/router/index'
import request from '@/utils/request'

/** 登录缓存里的当前用户：页面用它决定写按钮是否出现。 */
interface CurrentUser {
  id: number
  role: string
}

/** 建议系列弹窗的会议下拉（/meeting/selectOptions）。 */
interface MeetingOption {
  id: number
  meeting_no: string
  title: string
}

interface SeriesForm {
  id?: number
  name: string
  description: string
  cadence: string
  /** 编辑时原样带回，后端支持经 update 改状态（归档另有入口） */
  status?: string
}

interface SeriesPageState {
  user: CurrentUser
  name: string
  status: string
  cadence: string
  pageNum: number
  pageSize: number
  total: number
  tableData: MeetingSeriesItem[]
  loading: boolean
  formVisible: boolean
  form: SeriesForm
  rules: FormRules
  suggestVisible: boolean
  suggestMeetingId?: number
  suggestLoading: boolean
  suggested: boolean
  suggestions: SeriesSuggestionCandidate[]
  meetingOptions: MeetingOption[]
  detailVisible: boolean
  detailLoading: boolean
  detail: MeetingSeriesDetail | null
  detailTab: string
  detailMemories: MeetingMemoryItem[]
  detailCandidates: MeetingMemoryItem[]
  detailActionItems: ActionItem[]
}

const formRef = ref()
const user: CurrentUser = JSON.parse(localStorage.getItem('xm-user') || '{}')
const canManage = user.role === 'ADMIN'
// 后端口径：系列负责人或管理员可编辑；列表按钮按行判断，避免只有 ADMIN 能操作
const canManageRow = (row: { owner_id?: number }) =>
  user.role === 'ADMIN' || row.owner_id === user.id

const statusOptions = SERIES_STATUS.options
const cadenceOptions = SERIES_CADENCE.options
const statusText = SERIES_STATUS.text
const cadenceText = SERIES_CADENCE.text

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
const statusTag = SERIES_STATUS.tag as (status?: string) => TagType

const data = reactive<SeriesPageState>({
  user,
  name: '',
  status: '',
  cadence: '',
  pageNum: 1,
  pageSize: 10,
  total: 0,
  tableData: [],
  loading: false,
  formVisible: false,
  form: { name: '', description: '', cadence: 'WEEKLY' },
  rules: {
    name: [{ required: true, message: '请输入系列名称', trigger: 'blur' }],
    cadence: [{ required: true, message: '请选择周期', trigger: 'change' }],
  },
  suggestVisible: false,
  suggestMeetingId: undefined,
  suggestLoading: false,
  suggested: false,
  suggestions: [],
  meetingOptions: [],
  detailVisible: false,
  detailLoading: false,
  detail: null,
  detailTab: 'meetings',
  detailMemories: [],
  detailCandidates: [],
  detailActionItems: [],
})

const detailTitle = computed(() => data.detail?.name || '系列详情')

function load(): void {
  data.loading = true
  selectSeriesPage({
    name: data.name,
    status: data.status,
    cadence: data.cadence,
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
  data.name = ''
  data.status = ''
  data.cadence = ''
  data.pageNum = 1
  load()
}

function handleAdd(): void {
  data.form = { name: '', description: '', cadence: 'WEEKLY' }
  data.formVisible = true
}

function handleEdit(raw: unknown): void {
  // el-table 行插槽给的是 Record<PropertyKey, any>；在函数入口收窄成 MeetingSeriesItem
  const row = raw as MeetingSeriesItem
  data.form = {
    id: row.id,
    name: row.name,
    description: row.description || '',
    cadence: row.cadence,
    status: row.status,
  }
  data.formVisible = true
}

function save(): void {
  formRef.value.validate((valid: boolean) => {
    if (!valid) return
    const payload = {
      name: data.form.name,
      description: data.form.description || null,
      cadence: data.form.cadence,
    }
    const action = data.form.id
      ? updateSeries({ ...payload, id: data.form.id, status: data.form.status })
      : addSeries(payload)
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

function archive(raw: unknown): void {
  const row = raw as MeetingSeriesItem
  ElMessageBox.confirm(
    `归档后系列“${row.name}”不再参与建议与默认检索范围，会议与记忆数据保留。`,
    '归档系列',
    { type: 'warning' },
  )
    .then(() => archiveSeries(row.id))
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已归档')
        // 删除/归档最后一行时回退一页，避免停在空表
        if (data.tableData.length === 1 && data.pageNum > 1) data.pageNum -= 1
        load()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

function loadMeetingOptions(): void {
  if (data.meetingOptions.length) return
  request
    .get<MeetingOption[]>('/meeting/selectOptions')
    .then((res) => {
      data.meetingOptions = res.data || []
    })
    .catch(() => {})
}

function openSuggest(): void {
  data.suggestVisible = true
  data.suggestMeetingId = undefined
  data.suggestions = []
  data.suggested = false
  loadMeetingOptions()
}

function loadSuggestions(): void {
  if (!data.suggestMeetingId) return
  data.suggestLoading = true
  data.suggested = false
  suggestSeries(data.suggestMeetingId)
    .then((res) => {
      data.suggestions = res.data?.candidates || []
      data.suggested = true
    })
    .catch(() => {
      data.suggestions = []
    })
    .finally(() => (data.suggestLoading = false))
}

function applySuggestion(candidate: SeriesSuggestionCandidate): void {
  const meetingId = data.suggestMeetingId
  if (!meetingId) return
  const meeting = data.meetingOptions.find((item) => item.id === meetingId)
  ElMessageBox.confirm(
    `确定把会议“${meeting?.title || meetingId}”归入系列“${candidate.name}”吗？`,
    '应用建议',
    { type: 'warning' },
  )
    .then(() => assignMeetingToSeries(meetingId, candidate.series_id))
    .then((res) => {
      if (res.code === '200') {
        ElMessage.success('已归入系列')
        data.suggestVisible = false
        load()
      } else {
        ElMessage.error(res.msg)
      }
    })
    .catch(() => {})
}

async function loadDetailRelations(seriesId: number): Promise<void> {
  const [memories, candidates, actions] = await Promise.all([
    selectMemoryPage({
      scope_type: 'SERIES',
      scope_id: seriesId,
      status: 'CONFIRMED',
      pageSize: 100,
    }),
    selectMemoryPage({
      scope_type: 'SERIES',
      scope_id: seriesId,
      status: 'PROPOSED',
      pageSize: 100,
    }),
    // 看板展示全部状态，页面内再分列
    selectActionItemPage({ series_id: seriesId, pageSize: 100 }),
  ])
  data.detailMemories = memories.data?.list || []
  data.detailCandidates = candidates.data?.list || []
  data.detailActionItems = actions.data?.list || []
}

async function openDetail(id: number): Promise<void> {
  data.detailVisible = true
  data.detailLoading = true
  data.detailTab = 'meetings'
  data.detail = null
  data.detailMemories = []
  data.detailCandidates = []
  data.detailActionItems = []
  try {
    const res = await getSeriesDetail(id)
    data.detail = res.data
    await loadDetailRelations(id)
  } catch {
    // 详情加载失败：抽屉里显示空态，错误提示已由拦截器弹出
  } finally {
    data.detailLoading = false
  }
}

function openMeeting(meeting: { id: number }): void {
  router.push({ path: '/manager/meeting', query: { meetingId: String(meeting.id) } })
}

function openMeetingById(item: MeetingMemoryItem): void {
  if (!item.source_meeting_id) return
  router.push({
    path: '/manager/meeting',
    query: { meetingId: String(item.source_meeting_id) },
  })
}

function openActionItem(item: ActionItem): void {
  router.push({ path: '/manager/actionItems', query: { actionItemId: String(item.id) } })
}

load()
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
  gap: 8px;
}

.dialog-tip {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
  margin-bottom: 10px;
}

.suggest-actions {
  margin-top: 10px;
}

.suggest-list {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.suggest-item {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 12px;
}

.suggest-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.suggest-name {
  font-weight: 600;
  color: rgba(17, 24, 39, 0.9);
}

.suggest-score {
  font-size: 12px;
  color: #409eff;
}

.suggest-reasons {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: rgba(17, 24, 39, 0.6);
  line-height: 1.7;
}

.detail-wrap {
  min-height: 200px;
}

.detail-tabs {
  margin-top: 14px;
}
</style>
