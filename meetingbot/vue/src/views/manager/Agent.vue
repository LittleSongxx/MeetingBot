<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议主题模糊查询，后端先按标题查会议 ID，再用 ID 集合过滤运行记录 -->
      <el-input
        v-model="data.meetingTitle"
        style="width: 260px; margin-right: 10px"
        placeholder="请输入会议主题"
      ></el-input>
      <!-- 运行状态下拉，选项来自下面的 statusOptions 常量，清空后按全部状态查 -->
      <el-select
        v-model="data.status"
        clearable
        style="width: 175px; margin-right: 10px"
        placeholder="运行状态"
      >
        <el-option
          v-for="item in statusOptions"
          :key="item.value"
          :label="item.label"
          :value="item.value"
        />
      </el-select>
      <!-- 查询把页码重置成 1 再拉数据，重置额外清空两个查询条件 -->
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 发起入口不在这个页面，这条提示告诉用户去纪要页面点「Agent自检」 -->
      <span class="tip"
        >自检由 Agent
        对着转写原文检查纪要：六块内容有没有该记没记的、决策有没有负责人和完成时间、待办有没有跟进人。发现问题就自己改一版，改完再复核一遍。改出来的修订稿要人工确认后才会覆盖原纪要。发起入口在「会议纪要」页面的「Agent自检」按钮。</span
      >
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 表格数据来自 /agent/selectPage 返回的 list，每行是 run_dict 转出来的一条运行 -->
      <el-table stripe :data="data.rows">
        <el-table-column prop="run_no" label="运行编号" width="245" />
        <el-table-column
          prop="meeting_title"
          label="会议主题"
          min-width="170"
          show-overflow-tooltip
        />
        <!-- 同一场会可能有好几个录音各生成一份纪要，光看主题分不清自检的是哪一份 -->
        <el-table-column
          prop="material_name"
          label="音视频资料"
          min-width="190"
          show-overflow-tooltip
        />
        <!-- 进度条读取运行记录的 progress，运行状态决定进度颜色 -->
        <el-table-column label="进度" width="150">
          <template #default="scope">
            <el-progress
              :percentage="scope.row.progress"
              :status="
                scope.row.status === 'FAILED'
                  ? 'exception'
                  : ['SUCCEEDED', 'WAITING_CONFIRMATION', 'REJECTED'].includes(scope.row.status)
                    ? 'success'
                    : ''
              "
              :stroke-width="10"
            />
          </template>
        </el-table-column>
        <!-- 已完成轮次 / 轮数上限，后台每轮审查结束时 current_round 加一 -->
        <el-table-column label="轮次" width="80" align="center">
          <template #default="scope"
            >{{ scope.row.current_round }} / {{ scope.row.max_rounds }}</template
          >
        </el-table-column>
        <!-- 还没审查完第一轮时得分是 0，显示成短横线 -->
        <el-table-column label="得分" width="70" align="center">
          <template #default="scope">{{ scope.row.score || '-' }}</template>
        </el-table-column>
        <!-- 最后一轮审查挑出来的问题条数。自检通过时是 0，没通过时这些问题已经交给重写步改过一版 -->
        <el-table-column label="发现问题" width="90" align="center">
          <template #default="scope">
            <span :class="scope.row.issue_count ? 'issue-count' : ''">{{
              scope.row.issue_count
            }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="130">
          <template #default="scope">
            <el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" label="开始时间" width="175" />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="scope">
            <!-- 详情按钮打开运行详情弹窗，运行中的记录还会建立 SSE 连接 -->
            <el-button link type="primary" @click="showDetail(scope.row.id)">详情</el-button>
            <!-- 中断只对运行中的记录显示，恢复只对已中断的记录显示 -->
            <el-button
              v-if="scope.row.status === 'RUNNING'"
              link
              type="danger"
              @click="interrupt(scope.row.id)"
              >中断</el-button
            >
            <el-button
              v-if="scope.row.status === 'INTERRUPTED'"
              link
              type="success"
              @click="resume(scope.row.id)"
              >恢复</el-button
            >
            <!-- 运行中的记录后台还在写库，不显示删除 -->
            <el-button
              v-if="scope.row.status !== 'RUNNING'"
              link
              type="danger"
              @click="del(scope.row.id)"
              >删除</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </div>
    <!-- 有数据时才显示分页，翻页时 v-model 先改 data.pageNum，再触发 load 重新查询 -->
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

    <!-- 详情弹窗，内容全部来自 data.detail；弹窗关闭后调用 stopStream 断开事件流 -->
    <el-dialog
      title="纪要自检详情"
      v-model="data.detailVisible"
      width="76%"
      top="2vh"
      @closed="stopStream"
    >
      <el-descriptions v-if="data.detail.id" :column="3" border>
        <el-descriptions-item label="运行编号">{{ data.detail.run_no }}</el-descriptions-item>
        <el-descriptions-item label="会议主题">{{
          data.detail.meeting_title
        }}</el-descriptions-item>
        <el-descriptions-item label="音视频资料">{{
          data.detail.material_name || '-'
        }}</el-descriptions-item>
        <!-- stage 由后台每开一个步骤刷新一次，例如「第1轮：审查是否可落实」 -->
        <el-descriptions-item label="当前阶段">{{ data.detail.stage }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusTag(data.detail.status)">{{
            statusText(data.detail.status)
          }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="发起人">{{ data.detail.user_name }}</el-descriptions-item>
        <el-descriptions-item label="轮次"
          >{{ data.detail.current_round }} / {{ data.detail.max_rounds }}</el-descriptions-item
        >
        <el-descriptions-item label="自检得分">{{ data.detail.score || '-' }}</el-descriptions-item>
        <!-- 下面三项按条件显示：处理过修订稿才有确认人，放弃过才有原因，失败了才有失败原因 -->
        <el-descriptions-item v-if="data.detail.applier_name" label="确认人"
          >{{ data.detail.applier_name }} · {{ data.detail.applied_time }}</el-descriptions-item
        >
        <el-descriptions-item v-if="data.detail.reject_reason" label="放弃原因" :span="2">{{
          data.detail.reject_reason
        }}</el-descriptions-item>
        <el-descriptions-item v-if="data.detail.error_message" label="失败原因" :span="3">{{
          data.detail.error_message
        }}</el-descriptions-item>
      </el-descriptions>
      <div class="run-actions">
        <!-- 弹窗里的中断、恢复按钮和列表操作列调用同一组方法 -->
        <el-button
          v-if="data.detail.status === 'RUNNING'"
          type="danger"
          plain
          @click="interrupt(data.detail.id)"
          >中断运行</el-button
        >
        <el-button
          v-if="data.detail.status === 'INTERRUPTED'"
          type="success"
          plain
          @click="resume(data.detail.id)"
          >恢复运行</el-button
        >
        <!-- 等待确认状态下才出现这两个按钮，一个采纳修订稿，一个放弃 -->
        <template v-if="data.detail.status === 'WAITING_CONFIRMATION'">
          <el-button type="success" @click="apply(true)">用修订稿覆盖原纪要</el-button>
          <el-button type="danger" plain @click="apply(false)">放弃这次修订</el-button>
        </template>
      </div>

      <!-- 步骤时间线，steps 按 step_no 从小到大排列 -->
      <el-timeline class="steps">
        <el-timeline-item
          v-for="step in data.detail.steps || []"
          :key="step.id"
          :timestamp="step.create_time"
          :type="stepType(step.status)"
        >
          <div class="step-card">
            <div class="step-title">
              <!-- 步骤号在整次运行内连续递增，步骤类型翻成中文的审查、重写、汇总结论 -->
              步骤 {{ step.step_no }} · {{ stepTypeText(step.step_type) }}
              <el-tag size="small" :type="stepType(step.status)">{{ step.status }}</el-tag>
              <!-- 耗时来自 agent_step.elapsed_ms -->
              <span class="elapsed">{{ step.elapsed_ms }} ms</span>
            </div>
            <!-- 开步骤时写好的描述，例如「第1轮：按 4 条问题重写」 -->
            <div v-if="step.decision_summary" class="summary">{{ step.decision_summary }}</div>
            <!-- 审查步单独渲染：先一行判定汇总，再把每条问题做成卡片 -->
            <div v-if="step.step_type === 'REVIEW' && step.payload" class="review-body">
              <div class="review-head">
                判定：{{ step.payload.passed ? '通过' : '不通过' }} · 得分
                {{ step.payload.score }} · 问题 {{ (step.payload.issues || []).length }} 条
              </div>
              <div
                v-for="(issue, index) in step.payload.issues || []"
                :key="index"
                class="issue-item"
              >
                <!-- field 说明问题出在纪要哪个字段，issue_type 说明是哪一类问题 -->
                <el-tag size="small" type="warning">{{ fieldText(issue.field) }}</el-tag>
                <el-tag size="small" type="danger" style="margin-left: 6px">{{
                  issueTypeText(issue.issue_type)
                }}</el-tag>
                <!-- detail 是模型指出的具体问题，suggestion 是它给的改法 -->
                <div class="issue-detail">{{ issue.detail }}</div>
                <div class="issue-suggestion">建议：{{ issue.suggestion }}</div>
              </div>
            </div>
            <!-- 重写步和汇总步没有问题卡片，直接显示结论文本 -->
            <div v-else-if="step.observation_summary" class="observation">
              {{ step.observation_summary }}
            </div>
          </div>
        </el-timeline-item>
      </el-timeline>

      <!-- final_result 由汇总步骤写入，运行跑完才有 -->
      <div v-if="data.detail.final_result" class="final-result">
        <h3>自检结论</h3>
        <div>{{ data.detail.final_result }}</div>
      </div>

      <!-- 修订稿是一份完整纪要，revision 为空说明一个字都没改，这一块不显示 -->
      <div v-if="data.detail.revision" class="revision">
        <h3>修订稿预览</h3>
        <!-- 变更清单：后端按条目判据算出的"改了什么"。确认覆盖前先看这一块，
             六个字段的每一处增删改都在这里，不必拿整篇稿子肉眼比对 -->
        <div v-if="data.detail.change_list" class="revision-item change-list">
          <b>本次改动</b>
          <div class="change-summary">{{ data.detail.change_list.summary_line }}</div>
          <template v-for="(sections, field) in data.detail.change_list.per_field" :key="field">
            <div
              v-if="
                (sections.added && sections.added.length) ||
                (sections.removed && sections.removed.length) ||
                (sections.modified && sections.modified.length)
              "
              class="change-field"
            >
              <el-tag size="small">{{ fieldText(field) }}</el-tag>
              <div
                v-for="(text, i) in sections.added || []"
                :key="'a' + i"
                class="change-line added"
              >
                ＋ 新增：{{ text }}
              </div>
              <div
                v-for="(text, i) in sections.modified || []"
                :key="'m' + i"
                class="change-line modified"
              >
                ± 修改：{{ text }}
              </div>
              <div
                v-for="(text, i) in sections.removed || []"
                :key="'r' + i"
                class="change-line removed"
              >
                － 删除：{{ text }}
              </div>
            </div>
          </template>
        </div>
        <div class="revision-item">
          <b>会议摘要</b>
          <div>{{ data.detail.revision.summary }}</div>
        </div>
        <!-- 六个字段全部展示：apply 会整体覆盖六块内容，预览少一块等于盲改一块 -->
        <div class="revision-item">
          <b>议题</b>
          <div
            v-for="(item, index) in data.detail.revision.topics || []"
            :key="index"
            class="revision-line"
          >
            {{ index + 1 }}. {{ item.title
            }}<template v-if="item.summary"> —— {{ item.summary }}</template>
          </div>
        </div>
        <div class="revision-item">
          <b>发言观点</b>
          <div
            v-for="(item, index) in data.detail.revision.viewpoints || []"
            :key="index"
            class="revision-line"
          >
            {{ index + 1 }}. {{ item.speaker ? '[' + item.speaker + '] ' : '' }}{{ item.viewpoint }}
          </div>
        </div>
        <!-- 决策和待确认事项带上负责人与完成时间，模型没给出时显示未指定 -->
        <div class="revision-item">
          <b>决策</b>
          <div
            v-for="(item, index) in data.detail.revision.decisions || []"
            :key="index"
            class="revision-line"
          >
            {{ index + 1 }}. {{ item.content }}（负责人：{{
              item.owner_suggestion || '未指定'
            }}，完成时间：{{ item.deadline_suggestion || '未指定' }}）
          </div>
        </div>
        <div class="revision-item">
          <b>待确认事项</b>
          <div
            v-for="(item, index) in data.detail.revision.pending_items || []"
            :key="index"
            class="revision-line"
          >
            {{ index + 1 }}. {{ item.content }}（跟进人：{{
              item.owner_suggestion || '未指定'
            }}，完成时间：{{ item.deadline_suggestion || '未指定' }}）
          </div>
        </div>
        <div class="revision-item">
          <b>风险与争议</b>
          <div
            v-for="(item, index) in data.detail.revision.risks || []"
            :key="index"
            class="revision-line"
          >
            {{ index + 1 }}. {{ item.content
            }}<template v-if="item.suggestion">（建议：{{ item.suggestion }}）</template>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onUnmounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request'
// 分页信封的形状定义在 request 里（code/msg/data + PageInfo 的 total/list）
import type { PageData } from '@/utils/request'
import { AGENT_RUN_STATUS } from '@/constants/status'
// 步骤类型（REVIEW / REFINE / FINAL）翻中文，模板里的 stepTypeText 用它（与观测页共用的同一份）
import { stepTypeText } from '@/constants/label'

// 事件流用 fetch 直接请求，需要自己拼后端地址，取值来自 .env.development 的 VITE_BASE_URL
const baseUrl = import.meta.env.VITE_BASE_URL

// 运行列表的一行，来自 GET /agent/selectPage 返回的 data.list（后端的 run_dicts）
interface AgentRunRow {
  id: number
  run_no: string
  meeting_title: string | null
  // 这次自检的是哪份录音，同一场会可能有好几个素材
  material_name: string | null
  status: string
  progress: number
  current_round: number
  max_rounds: number
  score: number | null
  issue_count: number
  started_at: string
}

// 审查步 payload 里的一条问题（后端 REVIEW_SCHEMA 的条目）：出问题的字段、问题类型、具体问题与改法
interface AgentIssue {
  field?: string
  issue_type?: string
  detail?: string
  suggestion?: string
}

// 审查步的 payload：判定、得分和留下的问题清单。
// 重写步的 payload 是改完的整份纪要，页面不在那儿取字段，所以只声明审查步用到的这几个
interface AgentStepPayload {
  passed?: boolean
  score?: number
  issues?: AgentIssue[]
}

// 时间线的一个步骤节点，来自 GET /agent/selectById 返回的 detail.steps（后端的 step_dict）
interface AgentStep {
  id: number
  step_no: number
  step_type: string
  status: string
  decision_summary?: string | null
  observation_summary?: string | null
  payload?: AgentStepPayload | null
  elapsed_ms?: number | null
  create_time: string
}

// 变更清单里某一个内容字段的改动（后端 build_change_list 的 per_field[字段]）
interface AgentChangeSection {
  added?: string[]
  removed?: string[]
  modified?: string[]
}

// 变更清单：summary_line 是给人看的一句话，逐字段的改动在 per_field 里
// （键名与后端 build_change_list 的返回一致；此前模板读的是 detail，那个键不存在，
//  于是"本次改动"逐条列表一直渲染为空——字段名对不上时 v-for 会静默跳过）
interface AgentChangeList {
  summary_line?: string
  per_field?: Record<string, AgentChangeSection>
}

// 修订稿预览的条目（议题 / 发言观点）：revised_json 里存的是纪要契约的条目，
// 页面只读标题、正文这几个字符串字段
interface RevisionTopicItem {
  title?: string
  summary?: string
}
interface RevisionViewpointItem {
  speaker?: string
  viewpoint?: string
}
// 决策 / 待确认事项条目：负责人与完成时间在契约里是 owner / deadline 槽位，
// 页面读的是 present 之后的 owner_suggestion / deadline_suggestion（取不到就显示“未指定”）
interface RevisionActionItem {
  content?: string
  owner_suggestion?: string | null
  deadline_suggestion?: string | null
}
interface RevisionRiskItem {
  content?: string
  suggestion?: string
}

// 修订稿预览，来自 GET /agent/selectById 返回的 revision（agent_run.revised_json）
interface AgentRevision {
  summary?: string
  topics?: RevisionTopicItem[]
  viewpoints?: RevisionViewpointItem[]
  decisions?: RevisionActionItem[]
  pending_items?: RevisionActionItem[]
  risks?: RevisionRiskItem[]
}

// 一次自检运行的完整详情，来自 GET /agent/selectById（后端 run_dict(with_steps=True)）
interface AgentRunDetail {
  id: number
  run_no: string
  meeting_title: string | null
  material_name: string | null
  stage: string | null
  status: string
  user_name: string | null
  current_round: number
  max_rounds: number
  score: number | null
  // 处理过修订稿才有确认人和确认时间，放弃过才有原因
  applier_name: string | null
  applied_time: string | null
  reject_reason: string | null
  error_message: string | null
  // 下面几项带步骤的详情接口才返回：steps 是时间线，revision 是修订稿，change_list 是变更清单
  steps?: AgentStep[]
  final_result?: string
  revision?: AgentRevision | null
  change_list?: AgentChangeList | null
}

// 本页全部响应式状态；字段名与模板、脚本里的既有用法一一对应
interface AgentPageState {
  // 会议主题输入框绑定它，作为 meeting_title 参数发给后端
  meetingTitle: string
  // 运行状态下拉框绑定它，作为 status 参数发给后端
  status: string
  rows: AgentRunRow[]
  total: number
  pageNum: number
  pageSize: number
  detailVisible: boolean
  detail: AgentRunDetail
  // 当前事件流连接的 AbortController，stopStream 用它断开；没连接时是 null
  streamController: AbortController | null
  // 事件流断线重连次数，退避用；原先只在重连逻辑里往 data 上写，这里把字段补进状态声明
  streamRetries: number
}

const data = reactive<AgentPageState>({
  // 会议主题输入框绑定它，作为 meeting_title 参数发给后端
  meetingTitle: '',
  // 运行状态下拉框绑定它，作为 status 参数发给后端
  status: '',
  // 表格数据和分页信息，来自 /agent/selectPage 的返回
  rows: [],
  total: 0,
  pageNum: 1,
  pageSize: 10,
  // 详情弹窗的开关和内容，showDetail 和事件流往 detail 里写
  detailVisible: false,
  // 空对象只是接口回来之前的占位（模板用 data.detail.id 判断有没有内容），
  // 收窄成详情类型，模板里的 data.detail.xxx 才有类型
  detail: {} as AgentRunDetail,
  // 当前事件流连接的 AbortController，stopStream 用它断开
  streamController: null,
  // 重连退避的计数：连接成功时归零，失败时加一；初值就是退避公式里用的 0
  streamRetries: 0,
})
// 六种运行状态，既做下拉选项也做表格里的中文映射
// 状态字典唯一真源在 @/constants
// el-tag 的 :type 只接受这五个字面量，而 @/constants 里 tag 函数声明的返回值是 string
//（未知状态时实际返回空串，在 el-tag 上等于不指定颜色）；所以这里只收窄返回类型，
// 断言不改变运行时，模板调用的仍是 constants 里的同一个函数（与 Transcription.vue 同一处理）
type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'
const { options: statusOptions, text: statusText } = AGENT_RUN_STATUS
const statusTag = AGENT_RUN_STATUS.tag as (status?: string) => TagType
// 时间线节点和步骤标签的颜色，RUNNING 用默认的蓝色
const stepType = (value) => ({ SUCCEEDED: 'success', FAILED: 'danger' })[value] || 'primary'
// issue.field 指向纪要的六个内容字段，和后端 MINUTES_FIELDS 对应
const fieldText = (value) =>
  ({
    summary: '会议摘要',
    topics: '议题',
    viewpoints: '发言观点',
    decisions: '决策',
    pending_items: '待确认事项',
    risks: '风险与争议',
  })[value] || value
// issue.issue_type 的六种取值，和后端 REVIEW_SCHEMA 的枚举对应
const issueTypeText = (value) =>
  ({
    MISSING_OWNER: '缺负责人',
    MISSING_DEADLINE: '缺完成时间',
    NOT_ACTIONABLE: '无法执行',
    VAGUE: '表述含糊',
    UNSUPPORTED: '原文无依据',
    MISSING_ITEM: '纪要漏记',
  })[value] || value

// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.rows || data.rows.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () =>
  request
    // 分页信封：data 里是 { total, list }，list 的每行就是 run_dict 转出来的一条运行
    .get<PageData<AgentRunRow>>('/agent/selectPage', {
      // 四个查询参数：会议主题模糊词、状态、页码和每页条数
      params: {
        meeting_title: data.meetingTitle,
        status: data.status,
        pageNum: data.pageNum,
        pageSize: data.pageSize,
      },
    })
    .then((res) => {
      if (res.code === '200') {
        // list 填进表格，total 交给分页控件算页数
        data.rows = res.data?.list || []
        data.total = res.data?.total || 0
      } else ElMessage.error(res.msg)
    })

// 详情接口返回单条运行（带 steps/revision/change_list），正好是弹窗的 AgentRunDetail
const showDetail = (id: number) =>
  request.get<AgentRunDetail>('/agent/selectById/' + id).then((res) => {
    if (res.code === '200') {
      // 返回对象里带 steps 和 revision，弹窗的概况、时间线、修订稿预览都从它渲染
      data.detail = res.data
      data.detailVisible = true
      // 只有还在跑的运行才建立事件流，其他状态的数据不会再变化
      if (res.data.status === 'RUNNING') startStream(id)
    } else ElMessage.error(res.msg)
  })

const stopStream = () => {
  if (data.streamController) {
    // abort 让 fetch 和 reader.read() 抛出 AbortError，startStream 的读取循环随之结束
    data.streamController.abort()
    data.streamController = null
  }
}

// 用 fetch 读 SSE，服务端每有变化就推一份完整运行数据过来，页面不用定时轮询
const startStream = async (id: number) => {
  // 先断掉上一条连接，避免两条流同时往 data.detail 里写
  stopStream()
  const controller = new AbortController()
  data.streamController = controller
  // token 存在 localStorage 的 xm-user 里，EventSource 不能带自定义请求头，所以用 fetch
  const user = JSON.parse(localStorage.getItem('xm-user') || '{}')
  try {
    const response = await fetch(`${baseUrl}/agent/events/${id}`, {
      headers: { token: user.token || '' },
      // 挂上中断信号，stopStream 调 abort 就能断开
      signal: controller.signal,
    })
    // 权限不足等业务错误返回的是普通 JSON，不是事件流，这里当作连接失败处理
    if (
      !response.ok ||
      !response.body ||
      !response.headers.get('content-type')?.includes('text/event-stream')
    )
      throw new Error('SSE unavailable')
    // 连接建立成功：重连退避归零
    data.streamRetries = 0
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    // 一次读到的数据块可能只有半条报文，用 buffer 暂存到下一块到达
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      // 服务端进入结束状态后关闭连接，这里读到 done
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 报文之间用两个换行分隔，最后一段可能不完整，放回 buffer
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''
      for (const event of events) {
        const line = event.split('\n').find((item) => item.startsWith('data: '))
        if (line) {
          // 去掉 6 个字符的「data: 」前缀，剩下的是完整运行数据，整体替换弹窗内容
          const payload = JSON.parse(line.slice(6))
          if (payload.deleted) {
            stopStream()
            load()
            return
          }
          data.detail = payload
          // 列表里这一行的进度、轮次、得分、状态一起刷新。
          // 节流：重写步与审查步可能在数秒内推十几条事件，不节流会形成请求风暴
          throttledLoad()
        }
      }
    }
  } catch (error) {
    // 关弹窗、离开页面时主动 abort 也会进 catch，这种情况不重连
    // catch 到的值类型是 unknown，这里按 Error 收窄一次再读 name（运行时取值不变）
    if ((error as Error).name !== 'AbortError') {
      // 断线重连（指数退避，封顶 15s）：弹窗还开着且运行仍在跑才重连，
      // 否则退化为提示。此前断线后打开中的详情从此停更，只能手动关重开。
      const retries = (data.streamRetries = (data.streamRetries || 0) + 1)
      const delay = Math.min(15000, 1000 * 2 ** (retries - 1))
      if (data.detailVisible && data.detail && data.detail.status === 'RUNNING') {
        setTimeout(() => {
          if (data.detailVisible && data.detail && data.detail.status === 'RUNNING') startStream(id)
        }, delay)
      } else {
        ElMessage.error('自检实时连接已断开')
      }
    }
  } finally {
    // 只清理自己这条连接的控制器，不误清后面新建的连接
    if (data.streamController === controller) data.streamController = null
  }
}

// SSE 事件风暴的节流刷新：1 秒内多个事件只触发一次列表请求
let _lastLoadAt = 0
// 待触发的节流定时器；用 ReturnType 而不是写死 number/Timeout，
// 免得浏览器与 node 两套 setTimeout 的类型打架
let _loadTimer: ReturnType<typeof setTimeout> | null = null
const throttledLoad = () => {
  const now = Date.now()
  if (now - _lastLoadAt >= 1000) {
    _lastLoadAt = now
    load()
  } else if (!_loadTimer) {
    _loadTimer = setTimeout(
      () => {
        _loadTimer = null
        _lastLoadAt = Date.now()
        load()
      },
      1000 - (now - _lastLoadAt),
    )
  }
}

const apply = (approved: boolean) => {
  // 覆盖和放弃共用这个请求函数，approved 区分动作，reason 是放弃原因
  // reason 只在放弃路径有值，覆盖路径传 null
  const action = (reason: string | null) =>
    request.put('/agent/apply/' + data.detail.id, { approved, reason }).then((res) => {
      if (res.code === '200') {
        ElMessage.success(approved ? '修订稿已覆盖原纪要' : '已放弃这次修订')
        // 重新读取详情，弹窗显示 SUCCEEDED 或 REJECTED，以及确认人和放弃原因
        showDetail(data.detail.id)
        // 列表里这一行的状态跟着刷新
        load()
      } else ElMessage.error(res.msg)
    })
  if (approved)
    // 覆盖前弹确认框，点确定才以 approved=true、reason=null 提交
    ElMessageBox.confirm('修订稿会覆盖当前纪要内容，原内容不再保留，确定应用吗？', '人工确认', {
      type: 'warning',
    }).then(() => action(null))
  else
    // 放弃前弹输入框收集原因，点确定以 approved=false 和输入值提交
    ElMessageBox.prompt('请填写放弃原因', '放弃修订', {
      inputPlaceholder: '例如：修订内容与实际讨论不符',
    }).then(({ value }) => action(value))
}

const interrupt = (id: number) =>
  request.put('/agent/interrupt/' + id).then((res) => {
    if (res.code === '200') {
      ElMessage.success('自检已中断')
      // 运行不会再变化，先断开事件流，再读取一次详情显示「已中断」
      stopStream()
      showDetail(id)
      load()
    } else ElMessage.error(res.msg)
  })

const resume = (id: number) =>
  request.put('/agent/resume/' + id).then((res) => {
    if (res.code === '200') {
      ElMessage.success('自检已恢复运行')
      // 状态已改回 RUNNING，showDetail 会重新建立事件流
      showDetail(id)
      load()
    } else ElMessage.error(res.msg)
  })

const del = (id: number) =>
  ElMessageBox.confirm('删除后这次自检的全部步骤记录都会消失，确定删除吗？', '删除确认', {
    type: 'warning',
  }).then(() =>
    request.delete('/agent/delete/' + id).then((res) => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 重新拉一次列表，被删的那行消失
        reloadAfterDelete()
      } else ElMessage.error(res.msg)
    }),
  )

// 反思环是后台异步跑的，列表里只要还有在跑的运行就每三秒重拉一次，进度条自己往前走
const timer = window.setInterval(() => {
  if (data.rows.some((item) => item.status === 'RUNNING')) load()
}, 3000)

const search = () => {
  // 换了查询条件回到第一页
  data.pageNum = 1
  load()
}
const reset = () => {
  // 清空两个查询条件并回到第一页
  data.meetingTitle = ''
  data.status = ''
  data.pageNum = 1
  load()
}
// 页面打开时先拉一次列表
load()
onUnmounted(() => {
  // 离开页面时既要断开 SSE，也要清掉轮询定时器，否则路由切走了还在发请求
  stopStream()
  window.clearInterval(timer)
})
</script>

<style scoped>
.tip {
  color: rgba(17, 24, 39, 0.55);
  line-height: 1.7;
}
.issue-count {
  color: #ef4444;
  font-weight: bold;
}
.run-actions {
  margin: 14px 0;
}
.steps {
  margin-top: 12px;
}
.step-card {
  padding: 10px;
  background: #f8fafc;
  border-radius: 5px;
}
.step-title {
  font-weight: bold;
}
.elapsed {
  float: right;
  color: rgba(17, 24, 39, 0.55);
  font-weight: normal;
}
.summary {
  margin-top: 8px;
}
.observation {
  margin-top: 8px;
  white-space: pre-wrap;
  color: rgba(17, 24, 39, 0.75);
}
.review-body {
  margin-top: 10px;
}
.review-head {
  color: rgba(17, 24, 39, 0.75);
  margin-bottom: 8px;
}
.issue-item {
  padding: 9px 10px;
  margin-bottom: 8px;
  background: white;
  border: 1px solid rgba(17, 24, 39, 0.1);
  border-radius: 4px;
}
.issue-detail {
  margin-top: 6px;
}
.issue-suggestion {
  margin-top: 4px;
  color: rgba(17, 24, 39, 0.55);
}
.final-result {
  padding: 15px;
  background: #ecfdf5;
  border-left: 4px solid #10b981;
  white-space: pre-wrap;
  line-height: 1.8;
}
.revision {
  margin-top: 14px;
  padding: 15px;
  background: #f8fafc;
  border: 1px solid rgba(17, 24, 39, 0.1);
  border-radius: 5px;
}
.revision h3 {
  margin: 0 0 10px;
}
.revision-item {
  margin-bottom: 12px;
  line-height: 1.8;
}
.change-summary {
  margin: 4px 0;
  color: rgba(17, 24, 39, 0.75);
}
.change-field {
  margin: 6px 0 6px 4px;
}
.change-line {
  margin: 2px 0 2px 8px;
  font-size: 12px;
}
.change-line.added {
  color: #10b981;
}
.change-line.modified {
  color: #f59e0b;
}
.change-line.removed {
  color: #ef4444;
  text-decoration: line-through;
}
.revision-line {
  color: rgba(17, 24, 39, 0.75);
}
.revision-title {
  margin-left: 8px;
  font-weight: bold;
}
</style>
