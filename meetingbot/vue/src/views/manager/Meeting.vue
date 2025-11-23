<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议主题输入框，写入 data.title，后端按主题模糊匹配 -->
      <el-input
        v-model="data.title"
        style="width: 230px; margin-right: 10px"
        placeholder="请输入会议主题"
      ></el-input>
      <!-- 状态下拉框，选项来自 statusOptions，写入 data.status -->
      <el-select
        v-model="data.status"
        clearable
        style="width: 170px; margin-right: 10px"
        placeholder="请选择状态"
      >
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        ></el-option>
      </el-select>
      <!-- 系列筛选：与会议表单的系列下拉同源，清空表示全部系列 -->
      <el-select
        v-model="data.seriesId"
        clearable
        filterable
        style="width: 190px; margin-right: 10px"
        placeholder="全部系列"
      >
        <el-option
          v-for="item in data.seriesOptions"
          :key="item.id"
          :label="item.name"
          :value="item.id"
        ></el-option>
      </el-select>
      <!-- 视图下拉框，写入 data.viewType，决定后端按哪种范围过滤 -->
      <el-select v-model="data.viewType" style="width: 170px; margin-right: 10px">
        <!-- “全部会议”只对管理员出现，员工看不到这个选项 -->
        <el-option v-if="data.user.role === 'ADMIN'" label="全部会议" value="ALL"></el-option>
        <el-option label="我的会议" value="MY"></el-option>
        <el-option label="我创建的" value="CREATED"></el-option>
        <el-option label="我参与的" value="PARTICIPATED"></el-option>
      </el-select>
      <el-button type="info" plain @click="load">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-button type="primary" plain @click="handleAdd">创建会议</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="meeting_no" label="会议编号" width="210" />
        <!-- show-overflow-tooltip 让超长主题以省略号显示，鼠标悬停看全文 -->
        <el-table-column prop="title" label="会议主题" min-width="180" show-overflow-tooltip />
        <!-- 所属系列：未归类的会议显示占位符 -->
        <el-table-column label="会议系列" width="140" show-overflow-tooltip>
          <template #default="scope">{{ scope.row.series_name || '-' }}</template>
        </el-table-column>
        <el-table-column prop="start_time" label="开始时间" width="175" />
        <el-table-column prop="end_time" label="结束时间" width="175" />
        <el-table-column prop="location" label="地点" min-width="140" show-overflow-tooltip />
        <el-table-column prop="host_name" label="主持人" width="100" />
        <!-- participant_count 由后端统计参会记录条数得到 -->
        <el-table-column prop="participant_count" label="参会人数" width="90" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="scope">
            <!-- 用 statusTag 决定颜色，用 statusText 决定中文 -->
            <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="scope">
            <!-- 详情所有能看到这条数据的人都能点 -->
            <el-button link type="primary" @click="showDetail(scope.row.id)">详情</el-button>
            <!-- 资料跳到第 6 章的会议资料页 -->
            <el-button link type="primary" @click="showMaterial(scope.row.id)">资料</el-button>
            <!-- 编辑只在待开始且当前用户是管理员或创建人时出现 -->
            <el-button
              v-if="canEdit(scope.row)"
              link
              type="primary"
              @click="handleEdit(scope.row.id)"
              >编辑</el-button
            >
            <!-- 建议系列：按历史会议内容推荐候选系列，并给出可核对的理由 -->
            <el-button
              v-if="data.user.role === 'ADMIN' || scope.row.creator_id === data.user.id"
              link
              type="primary"
              @click="openSuggest(scope.row)"
              >建议系列</el-button
            >
            <!-- 待开始的会议可以“开始” -->
            <el-button
              v-if="canManage(scope.row) && scope.row.status === 'SCHEDULED'"
              link
              type="success"
              @click="changeStatus(scope.row, 'IN_PROGRESS')"
              >开始</el-button
            >
            <!-- 进行中的会议可以“结束” -->
            <el-button
              v-if="canManage(scope.row) && scope.row.status === 'IN_PROGRESS'"
              link
              type="success"
              @click="changeStatus(scope.row, 'FINISHED')"
              >结束</el-button
            >
            <!-- 待开始的会议可以“取消”，进行中和已结束的都不能取消 -->
            <el-button
              v-if="canManage(scope.row) && scope.row.status === 'SCHEDULED'"
              link
              type="danger"
              @click="cancelMeeting(scope.row)"
              >取消</el-button
            >
            <!-- 删除会连带清掉这场会议下的全部业务数据，只开放给管理员 -->
            <el-button v-if="data.user.role === 'ADMIN'" link type="danger" @click="del(scope.row)"
              >删除</el-button
            >
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

    <!-- 标题按 data.form.id 是否存在切换成“编辑会议”或“创建会议” -->
    <el-dialog
      :title="data.form.id ? '编辑会议' : '创建会议'"
      v-model="data.formVisible"
      width="55%"
      destroy-on-close
    >
      <el-form
        ref="formRef"
        :model="data.form"
        :rules="data.rules"
        label-width="90px"
        style="padding: 10px 20px"
      >
        <!-- 会议主题，必填，写入 data.form.title -->
        <el-form-item label="会议主题" prop="title">
          <el-input v-model="data.form.title" placeholder="请输入会议主题"></el-input>
        </el-form-item>
        <!-- 所属系列，选填；留空表示暂不归类 -->
        <el-form-item label="会议系列">
          <el-select
            v-model="data.form.series_id"
            clearable
            filterable
            style="width: 100%"
            placeholder="请选择会议系列（可留空）"
          >
            <el-option
              v-for="item in data.seriesOptions"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            ></el-option>
          </el-select>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <!-- 开始时间，必填。value-format 决定提交给后端的字符串格式 -->
            <el-form-item label="开始时间" prop="start_time">
              <el-date-picker
                v-model="data.form.start_time"
                type="datetime"
                value-format="YYYY-MM-DD HH:mm:ss"
                style="width: 100%"
                placeholder="请选择开始时间"
              ></el-date-picker>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <!-- 结束时间，必填，后端还会校验它必须晚于开始时间 -->
            <el-form-item label="结束时间" prop="end_time">
              <el-date-picker
                v-model="data.form.end_time"
                type="datetime"
                value-format="YYYY-MM-DD HH:mm:ss"
                style="width: 100%"
                placeholder="请选择结束时间"
              ></el-date-picker>
            </el-form-item>
          </el-col>
        </el-row>
        <!-- 会议地点，选填，写入 data.form.location -->
        <el-form-item label="会议地点">
          <el-input
            v-model="data.form.location"
            placeholder="请输入会议室或线上会议地址"
          ></el-input>
        </el-form-item>
        <!-- 主持人单选下拉，必填，写入 data.form.host_id -->
        <el-form-item label="主持人" prop="host_id">
          <el-select
            v-model="data.form.host_id"
            filterable
            style="width: 100%"
            placeholder="请选择主持人"
          >
            <el-option
              v-for="item in data.candidates"
              :key="item.id"
              :value="item.id"
              :label="candidateLabel(item)"
            ></el-option>
          </el-select>
        </el-form-item>
        <!-- 参会人员多选下拉，写入 data.form.participant_ids 数组 -->
        <!-- collapse-tags 让选中很多人时折叠显示，避免撑开弹窗 -->
        <el-form-item label="参会人员">
          <el-select
            v-model="data.form.participant_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            style="width: 100%"
            placeholder="请选择参会人员"
          >
            <el-option
              v-for="item in data.candidates"
              :key="item.id"
              :value="item.id"
              :label="candidateLabel(item)"
            ></el-option>
          </el-select>
        </el-form-item>
        <!-- 会议议程文本域，写入 data.form.agenda -->
        <el-form-item label="会议议程">
          <el-input
            v-model="data.form.agenda"
            type="textarea"
            :rows="5"
            placeholder="请输入会议议程"
          ></el-input>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog title="会议详情" v-model="data.detailVisible" width="58%">
      <!-- data.detail.id 存在时才渲染，避免弹窗打开瞬间显示空白字段 -->
      <el-descriptions v-if="data.detail.id" :column="2" border>
        <el-descriptions-item label="会议编号">{{ data.detail.meeting_no }}</el-descriptions-item>
        <el-descriptions-item label="状态"
          ><el-tag :type="statusTag(data.detail.status)">{{
            statusText(data.detail.status)
          }}</el-tag></el-descriptions-item
        >
        <el-descriptions-item label="会议主题" :span="2">{{
          data.detail.title
        }}</el-descriptions-item>
        <el-descriptions-item label="会议系列">{{
          data.detail.series_name || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ data.detail.start_time }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ data.detail.end_time }}</el-descriptions-item>
        <el-descriptions-item label="创建人">{{ data.detail.creator_name }}</el-descriptions-item>
        <el-descriptions-item label="主持人">{{ data.detail.host_name }}</el-descriptions-item>
        <!-- 地点和议程为空时显示占位符 -->
        <el-descriptions-item label="会议地点" :span="2">{{
          data.detail.location || '-'
        }}</el-descriptions-item>
        <el-descriptions-item label="会议议程" :span="2">{{
          data.detail.agenda || '-'
        }}</el-descriptions-item>
        <!-- 只有取消过的会议才有取消原因，这一行才渲染 -->
        <el-descriptions-item v-if="data.detail.cancel_reason" label="取消原因" :span="2">{{
          data.detail.cancel_reason
        }}</el-descriptions-item>
      </el-descriptions>
      <div style="font-weight: bold; margin: 20px 0 10px">参会人员</div>
      <!-- 数据来自 meeting_dict 里的 participants 数组 -->
      <el-table :data="data.detail.participants || []" size="small">
        <el-table-column prop="name" label="姓名" />
        <el-table-column prop="department_name" label="部门" />
        <el-table-column prop="position" label="职位" />
        <el-table-column prop="participant_role" label="会议角色">
          <template #default="scope">{{
            participantRoleText(scope.row.participant_role)
          }}</template>
        </el-table-column>
        <el-table-column prop="response_status" label="参会状态">
          <template #default="scope">{{ responseStatusText(scope.row.response_status) }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 建议系列：为这场会议推荐候选系列 -->
    <el-dialog title="建议系列" v-model="data.suggestVisible" width="620px" destroy-on-close>
      <div class="suggest-tip">
        会议「{{ data.suggestMeetingTitle }}」可能属于以下系列（按历史内容相似度排序）。
        归入系列会写回会议，只有仍可编辑（待开始）的会议能改。
      </div>
      <div v-loading="data.suggestLoading">
        <div v-if="data.suggestions.length" class="suggest-list">
          <div
            v-for="candidate in data.suggestions"
            :key="candidate.series_id"
            class="suggest-item"
          >
            <div class="suggest-head">
              <span class="suggest-name">{{ candidate.name }}</span>
              <span class="suggest-score">相似度 {{ formatPercent(candidate.score) }}</span>
              <el-button link type="primary" @click="applySuggestion(candidate)">
                归入该系列
              </el-button>
            </div>
            <ul v-if="candidate.reasons?.length" class="suggest-reasons">
              <li v-for="(reason, index) in candidate.reasons" :key="index">{{ reason }}</li>
            </ul>
          </div>
        </div>
        <el-empty
          v-else-if="data.suggested"
          description="没有找到合适的候选系列"
          :image-size="60"
        />
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { h } from 'vue'
import request from '@/utils/request'
import type { PageData } from '@/utils/request'
import {
  assignMeetingToSeries,
  seriesOptions,
  suggestSeries,
  type SeriesOption,
  type SeriesSuggestionCandidate,
} from '@/api/series'
import { formatPercent } from '@/utils/format'
import { MEETING_STATUS, INVITATION_STATUS_TEXT, PARTICIPANT_ROLE_TEXT } from '@/constants/status'
import router from '@/router/index'

// ---- 后端返回结构（字段名与 fastapi-app/api/meeting.py 的 meeting_dict / participant_dict 一致）----

// 会议对象：列表行、详情弹窗、编辑回填都用它
interface MeetingItem {
  // 主键：详情、编辑、状态变更、删除都拼在请求地址里
  id: number
  meeting_no: string
  title: string
  // 后端已格式化成东八区的字符串
  start_time: string
  end_time: string
  location: string | null
  agenda: string | null
  // canManage/canEdit 用它判断当前用户是不是创建人
  creator_id: number
  creator_name: string | null
  // 编辑弹窗回填主持人下拉框；canManage 用它判断当前用户是不是主持人
  host_id: number
  host_name: string | null
  // 所属会议系列；未归类时为 null
  series_id: number | null
  series_name: string | null
  // 接口返回的状态码，交给 MEETING_STATUS 翻中文、取标签颜色
  status: string
  // 只有取消过的会议才有值，详情弹窗据此决定那一行是否渲染
  cancel_reason: string | null
  // 编辑弹窗回填参会人员多选框
  participant_ids: number[]
  // 详情弹窗的参会人员表格
  participants: MeetingParticipant[]
  // 列表“参会人数”列，后端统计参会记录条数得到
  participant_count: number
}

// 详情弹窗里参会人员表格的一行
interface MeetingParticipant {
  id: number
  // 与当前登录用户比对，也是编辑弹窗回填多选框的值
  user_id: number
  name: string | null
  department_name: string | null
  position: string | null
  // 交给 PARTICIPANT_ROLE_TEXT 翻中文
  participant_role: string
  // 交给 INVITATION_STATUS_TEXT 翻中文
  response_status: string
}

// 主持人和参会人员两个下拉框共用的候选项（/user/selectMeetingCandidates 返回）
interface MeetingCandidate {
  // 选项 value，最终写进 meeting.host_id 或 meeting_participant.user_id
  id: number
  name: string
  department_name: string | null
  position: string | null
}

// 创建/编辑弹窗的表单模型：只需提交的字段，编辑时带 id
interface MeetingForm {
  id?: number
  title?: string
  start_time?: string
  end_time?: string
  location?: string | null
  host_id?: number
  participant_ids?: number[]
  agenda?: string | null
  // 所属系列；清空时归一化成 null 再提交
  series_id?: number | null
}

// 删除确认框里的连带数据统计（/meeting/deletePreview/{meeting_id} 返回）
interface MeetingDeletePreview {
  // 确认框标题里的会议主题
  title: string
  participant_count: number
  material_count: number
  transcription_count: number
  segment_count: number
  minutes_count: number
  agent_run_count: number
}

// 弹窗必填校验规则里的一条
interface FormRule {
  required: boolean
  message: string
  trigger: string
}

// 顶部视图下拉框的取值范围，“全部会议”只对管理员出现
type MeetingViewType = 'ALL' | 'MY' | 'CREATED' | 'PARTICIPATED'

// 顶部状态下拉框的取值：MEETING_STATUS 的四个状态，清空后是空串
type MeetingStatusFilter = 'SCHEDULED' | 'IN_PROGRESS' | 'FINISHED' | 'CANCELLED' | ''

// 登录时写入 xm-user 缓存的当前用户，页面只用 id（判断是不是创建人/主持人）和 role（控制按钮与视图选项）
interface CurrentUser {
  id: number
  role: string
}

// 页面状态：顶部筛选条件、列表分页和两个弹窗的数据
interface MeetingPageState {
  // 当前登录用户，模板里用 data.user.role 做判断
  user: CurrentUser
  // 顶部会议主题输入框
  title: string
  // 顶部状态下拉框
  status: MeetingStatusFilter
  // 顶部视图下拉框。管理员默认看全部会议，员工默认看我的会议
  viewType: MeetingViewType
  // 顶部系列筛选；null 表示全部系列
  seriesId: number | null
  // 会议表单/系列筛公共的系列下拉选项
  seriesOptions: SeriesOption[]
  // 建议系列弹窗
  suggestVisible: boolean
  suggestMeetingId: number | null
  suggestMeetingTitle: string
  suggestLoading: boolean
  // 是否已经拿到过一次建议结果（用来区分"还没查"与"查了没有候选"）
  suggested: boolean
  suggestions: SeriesSuggestionCandidate[]
  // 当前页码
  pageNum: number
  // 每页条数
  pageSize: number
  // 总条数
  total: number
  // 表格数据源
  tableData: MeetingItem[]
  // 主持人和参会人员两个下拉框共用的候选人列表
  candidates: MeetingCandidate[]
  // 控制创建/编辑弹窗显示
  formVisible: boolean
  // 控制详情弹窗显示
  detailVisible: boolean
  // 创建/编辑弹窗的表单对象，有 id 表示编辑
  form: MeetingForm
  // 详情弹窗展示的会议对象。打开弹窗前是空对象，所以字段全可选
  detail: Partial<MeetingItem>
  // 弹窗必填校验规则
  rules: Record<string, FormRule[]>
}

const formRef = ref()
// 第 1 章登录时写入的缓存，页面用它判断按钮是否显示、默认视图取哪个
const user: CurrentUser = JSON.parse(localStorage.getItem('xm-user') || '{}')

const data = reactive<MeetingPageState>({
  // 当前登录用户，模板里用 data.user.role 做判断
  user,
  // 顶部会议主题输入框
  title: '',
  // 顶部状态下拉框
  status: '',
  // 顶部视图下拉框。管理员默认看全部会议，员工默认看我的会议
  viewType: user.role === 'ADMIN' ? 'ALL' : 'MY',
  // 顶部系列筛选（空表示全部）
  seriesId: null,
  // 系列下拉选项
  seriesOptions: [],
  // 建议系列弹窗
  suggestVisible: false,
  suggestMeetingId: null,
  suggestMeetingTitle: '',
  suggestLoading: false,
  suggested: false,
  suggestions: [],
  // 当前页码
  pageNum: 1,
  // 每页条数
  pageSize: 10,
  // 总条数
  total: 0,
  // 表格数据源
  tableData: [],
  // 主持人和参会人员两个下拉框共用的候选人列表
  candidates: [],
  // 控制创建/编辑弹窗显示
  formVisible: false,
  // 控制详情弹窗显示
  detailVisible: false,
  // 创建/编辑弹窗的表单对象，有 id 表示编辑
  form: {},
  // 详情弹窗展示的会议对象
  detail: {},
  // 弹窗必填校验规则
  rules: {
    title: [{ required: true, message: '请输入会议主题', trigger: 'blur' }],
    start_time: [{ required: true, message: '请选择开始时间', trigger: 'change' }],
    end_time: [{ required: true, message: '请选择结束时间', trigger: 'change' }],
    host_id: [{ required: true, message: '请选择主持人', trigger: 'change' }],
  },
})

// 状态字典唯一真源在 @/constants（Home 页的同键旧副本已与本版漂移，以本版为准）
const { options: statusOptions, text: statusText } = MEETING_STATUS
// el-tag 的 type 只接受这五档颜色；常量模块的 tag 签名是 (status?: string) => string，
// 会议状态只有 MEETING_STATUS 里登记的四个（颜色一定取自这五档），所以在页面边界上把返回类型收窄。
// 断言只作用于类型：运行时仍是 MEETING_STATUS.tag 本身，返回值与改动前完全一致
type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
const statusTag = MEETING_STATUS.tag as (status?: string) => TagType
// 详情弹窗里 participant_role / response_status 的中文
const participantRoleText = PARTICIPANT_ROLE_TEXT
const responseStatusText = INVITATION_STATUS_TEXT
// 人员下拉框的显示文案，把姓名、部门、职位拼起来，空值自动跳过
const candidateLabel = (item: MeetingCandidate) =>
  [item.name, item.department_name, item.position].filter(Boolean).join(' / ')

// 能变更状态的人：管理员、会议创建人、会议主持人
// 行参数沿用隐式 any：el-table 的行插槽给的是 element-plus 的 DefaultRow（Record<PropertyKey, any>），
// 声明成 MeetingItem 会让模板里的 canManage(scope.row) 传参失败，而不改模板绑定的前提下无法收窄
const canManage = (row) =>
  data.user.role === 'ADMIN' || row.creator_id === data.user.id || row.host_id === data.user.id
// 能编辑的人：会议还是待开始状态，且当前用户是管理员或创建人；主持人不能编辑会议内容
const canEdit = (row) =>
  row.status === 'SCHEDULED' && (data.user.role === 'ADMIN' || row.creator_id === data.user.id)

// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.tableData || data.tableData.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () => {
  request
    .get<PageData<MeetingItem>>('/meeting/selectPage', {
      params: {
        // 会议主题输入框的值
        title: data.title,
        // 状态下拉框的值
        status: data.status,
        // 视图下拉框的值，决定后端的数据范围
        view_type: data.viewType,
        // 系列筛选；清空后可能是空串，转成 undefined 让 Axios 不拼这个参数
        series_id: typeof data.seriesId === 'number' ? data.seriesId : undefined,
        // 当前页码
        page_num: data.pageNum,
        // 每页条数
        page_size: data.pageSize,
      },
    })
    .then((res) => {
      if (res.code === '200') {
        // 当前页会议写入表格数据源
        data.tableData = res.data?.list || []
        // 总条数写入分页条
        data.total = res.data?.total || 0
      } else {
        ElMessage.error(res.msg)
      }
    })
}

const loadCandidates = () => {
  // 主持人和参会人员两个下拉框共用这一份候选人数据
  request.get<MeetingCandidate[]>('/user/selectMeetingCandidates').then((res) => {
    if (res.code === '200') {
      data.candidates = res.data || []
    }
  })
}

/** 系列下拉选项与候选人一样按需加载：不进弹窗就不发这个请求 */
const loadSeriesOptions = () => {
  if (data.seriesOptions.length) return
  seriesOptions()
    .then((res) => {
      data.seriesOptions = res.data || []
    })
    .catch(() => {})
}

/** 打开"建议系列"弹窗并立即请求推荐：候选择序与理由都由后端算好 */
const openSuggest = (row) => {
  data.suggestMeetingId = row.id
  data.suggestMeetingTitle = row.title
  data.suggestions = []
  data.suggested = false
  data.suggestVisible = true
  data.suggestLoading = true
  suggestSeries(row.id)
    .then((res) => {
      data.suggestions = res.data?.candidates || []
      data.suggested = true
    })
    .catch(() => {
      data.suggestions = []
    })
    .finally(() => (data.suggestLoading = false))
}

/** 应用建议：复用会议更新接口把 series_id 写进会议 */
const applySuggestion = (candidate: SeriesSuggestionCandidate) => {
  const meetingId = data.suggestMeetingId
  if (!meetingId) return
  ElMessageBox.confirm(`确定把这场会议归入系列“${candidate.name}”吗？`, '应用建议', {
    type: 'warning',
  })
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

const handleAdd = () => {
  // 新表单没有 id，保存时走新增分支
  // 主持人默认是自己，参会人员默认也把自己放进去
  data.form = {
    host_id: data.user.id,
    participant_ids: [data.user.id],
    // 新建时默认不归类；清空下拉框时也回到 null
    series_id: null,
  }
  data.formVisible = true
  // 打开弹窗时才拉候选人列表与系列选项
  loadCandidates()
  loadSeriesOptions()
}

const handleEdit = (id: number) => {
  // 编辑按钮只传会议ID，这里请求 GET /meeting/selectById/{id} 取服务器上的最新会议数据
  // 返回值里的 participant_ids 回填参会人员多选框，host_id 回填主持人下拉框
  request.get<MeetingItem>('/meeting/selectById/' + id).then((res) => {
    if (res.code === '200') {
      // 深拷贝一份作为表单模型，弹窗里的改动不影响表格
      data.form = JSON.parse(JSON.stringify(res.data))
      // 老接口可能还不返回 series_id：显式补 null，保证下拉框受控
      if (typeof data.form.series_id !== 'number') data.form.series_id = null
      data.formVisible = true
      loadCandidates()
      loadSeriesOptions()
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const save = () => {
  // 先跑主题、起止时间、主持人四条必填校验
  formRef.value.validate((valid) => {
    if (!valid) {
      return
    }
    // 系列清空时 v-model 可能是空串：归一化成 null，避免后端把空串当 int 解析
    const payload = {
      ...data.form,
      series_id: typeof data.form.series_id === 'number' ? data.form.series_id : null,
    }
    // 有 id 走编辑，没有 id 走新增
    const action = data.form.id
      ? request.put('/meeting/update', payload)
      : request.post('/meeting/add', payload)
    action.then((res) => {
      if (res.code === '200') {
        ElMessage.success('保存成功')
        data.formVisible = false
        // 重新拉当前页，新建或修改的会议立即出现在表格里
        load()
      } else {
        // 例如“会议结束时间必须晚于开始时间”“只有待开始会议可以编辑”
        ElMessage.error(res.msg)
      }
    })
  })
}

const showDetail = (id: number) => {
  request.get<MeetingItem>('/meeting/selectById/' + id).then((res) => {
    if (res.code === '200') {
      // 整个会议对象赋给 data.detail，详情弹窗直接读它
      data.detail = res.data
      data.detailVisible = true
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const showMaterial = (id: number) => {
  // 把会议ID放进路由查询参数，第 6 章的会议资料页读到后直接选中这场会议
  router.push({ path: '/manager/meetingMaterial', query: { meetingId: id } })
}

const changeStatus = (row, status: string) => {
  // 同一个方法处理开始和结束，按目标状态决定提示文案
  const actionName = status === 'IN_PROGRESS' ? '开始' : '结束'
  ElMessageBox.confirm(`确定${actionName}会议“${row.title}”吗？`, '状态确认', {
    type: 'warning',
  }).then(() => {
    // 会议ID拼在地址里，请求体只带目标状态
    request.put('/meeting/changeStatus/' + row.id, { status }).then((res) => {
      if (res.code === '200') {
        ElMessage.success(actionName + '会议成功')
        // 重新拉列表，这一行的状态标签和操作按钮随之变化
        load()
      } else {
        ElMessage.error(res.msg)
      }
    })
  })
}

// 删除前先向后端要一份连带数据统计，让管理员看清这一刀会带走多少东西
const del = (row) => {
  // 会议行的 id 拼进地址，GET /meeting/deletePreview/{meeting_id} 只统计条数，不删除任何数据
  request.get<MeetingDeletePreview>('/meeting/deletePreview/' + row.id).then((res) => {
    // 预览失败（例如会议已被删除）时弹出后端提示，不再打开确认框
    if (res.code !== '200') {
      ElMessage.error(res.msg)
      return
    }
    // res.data 是 delete_preview 返回的会议主题 title 和六项条数
    const preview = res.data || {}
    // 逐项对照后端返回的条数，只列出确实有数据的项，没有的不占篇幅
    // satisfies 只做类型检查、不改变运行时：每一项固定是“名称/条数/单位”三元组，
    // 这样 .filter 里的 item[1] 仍是 number，可以直接和 0 比较
    const items = (
      [
        ['参会人员', preview.participant_count, '条'],
        ['会议资料', preview.material_count, '个（含磁盘上的文件）'],
        ['转写任务', preview.transcription_count, '个'],
        ['转写分段', preview.segment_count, '条'],
        ['会议纪要', preview.minutes_count, '份'],
        ['Agent运行记录', preview.agent_run_count, '次（含每一步的审查和重写记录）'],
      ] satisfies [string, number, string][]
    ).filter((item) => item[1] > 0)
    // 标题是用户可任意输入的字段：拼进 HTML 会被注入脚本（存储型 XSS）。
    // 这里改为 VNode 渲染——结构与样式不变，插值走 Vue 的文本转义。
    const detailLines = items.length
      ? [
          '以下数据会一并删除，删除后无法恢复：',
          ...items.map((item) => `${item[0]} ${item[1]} ${item[2]}`),
        ]
      : ['这场会议下还没有产生资料、转写和纪要数据。']
    ElMessageBox.confirm(
      h('div', null, [
        h('div', null, `确定删除会议“${preview.title}”吗？`),
        h('div', { style: 'margin-top: 8px' }, detailLines[0]),
        h(
          'ul',
          { style: 'margin: 6px 0 0 0; padding-left: 20px' },
          items.map((item) => h('li', null, `${item[0]} ${item[1]} ${item[2]}`)),
        ),
      ]),
      '删除会议',
      { type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消' },
    ).then(() => {
      // 点“确定删除”后才发 DELETE /meeting/delete/{meeting_id}；点“取消”时 Promise 不进入 then，不发请求
      request.delete('/meeting/delete/' + row.id).then((result) => {
        if (result.code === '200') {
          ElMessage.success('删除成功')
          // 重新拉当前页，被删的会议从表格消失，data.total 同步减少
          reloadAfterDelete()
        } else {
          ElMessage.error(result.msg)
        }
      })
    })
  })
}

const cancelMeeting = (row) => {
  // 用 prompt 弹出一个带输入框的确认框，让用户填取消原因
  ElMessageBox.prompt('请输入取消原因', '取消会议', {
    // 至少要有一个非空白字符，否则输入框下方提示且不关闭
    inputPattern: /\S+/,
    inputErrorMessage: '请输入取消原因',
  }).then(({ value }) => {
    request
      .put('/meeting/changeStatus/' + row.id, {
        status: 'CANCELLED',
        // 用户填的原因作为 cancel_reason 提交，最终存进 meeting.cancel_reason
        cancel_reason: value,
      })
      .then((res) => {
        if (res.code === '200') {
          ElMessage.success('会议已取消')
          load()
        } else {
          ElMessage.error(res.msg)
        }
      })
  })
}

const reset = () => {
  // 清空主题输入框
  data.title = ''
  // 清空状态下拉框
  data.status = ''
  // 视图回到各自角色的默认值，而不是清空
  data.viewType = data.user.role === 'ADMIN' ? 'ALL' : 'MY'
  // 系列筛选回到全部
  data.seriesId = null
  // 回到第一页
  data.pageNum = 1
  load()
}

loadSeriesOptions()
load()

// 从系列页/待办页带 meetingId 进来时，直接打开这场会议的详情
const meetingIdParam = Number(router.currentRoute.value.query.meetingId)
if (Number.isFinite(meetingIdParam) && meetingIdParam > 0) showDetail(meetingIdParam)
</script>

<style scoped>
.suggest-tip {
  font-size: 13px;
  color: rgba(17, 24, 39, 0.55);
  margin-bottom: 12px;
}

.suggest-list {
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
</style>
