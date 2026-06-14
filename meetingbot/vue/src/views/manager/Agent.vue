<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议主题模糊查询，后端先按标题查会议 ID，再用 ID 集合过滤运行记录 -->
      <el-input v-model="data.meetingTitle" style="width: 260px; margin-right: 10px" placeholder="请输入会议主题"></el-input>
      <!-- 运行状态下拉，选项来自下面的 statusOptions 常量，清空后按全部状态查 -->
      <el-select v-model="data.status" clearable style="width: 175px; margin-right: 10px" placeholder="运行状态">
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value" />
      </el-select>
      <!-- 查询把页码重置成 1 再拉数据，重置额外清空两个查询条件 -->
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 发起入口不在这个页面，这条提示告诉用户去纪要页面点「Agent自检」 -->
      <span class="tip">自检由 Agent 对着转写原文检查纪要：六块内容有没有该记没记的、决策有没有负责人和完成时间、待办有没有跟进人。发现问题就自己改一版，改完再复核一遍。改出来的修订稿要人工确认后才会覆盖原纪要。发起入口在「AI会议纪要」页面的「Agent自检」按钮。</span>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 表格数据来自 /agent/selectPage 返回的 list，每行是 run_dict 转出来的一条运行 -->
      <el-table stripe :data="data.rows">
        <el-table-column prop="run_no" label="运行编号" width="245" />
        <el-table-column prop="meeting_title" label="会议主题" min-width="170" show-overflow-tooltip />
        <!-- 同一场会可能有好几个录音各生成一份纪要，光看主题分不清自检的是哪一份 -->
        <el-table-column prop="material_name" label="音视频资料" min-width="190" show-overflow-tooltip />
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
          <template #default="scope">{{ scope.row.current_round }} / {{ scope.row.max_rounds }}</template>
        </el-table-column>
        <!-- 还没审查完第一轮时得分是 0，显示成短横线 -->
        <el-table-column label="得分" width="70" align="center">
          <template #default="scope">{{ scope.row.score || '-' }}</template>
        </el-table-column>
        <!-- 最后一轮审查挑出来的问题条数。自检通过时是 0，没通过时这些问题已经交给重写步改过一版 -->
        <el-table-column label="发现问题" width="90" align="center">
          <template #default="scope">
            <span :class="scope.row.issue_count ? 'issue-count' : ''">{{ scope.row.issue_count }}</span>
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
            <el-button v-if="scope.row.status === 'RUNNING'" link type="danger" @click="interrupt(scope.row.id)">中断</el-button>
            <el-button v-if="scope.row.status === 'INTERRUPTED'" link type="success" @click="resume(scope.row.id)">恢复</el-button>
            <!-- 运行中的记录后台还在写库，不显示删除 -->
            <el-button v-if="scope.row.status !== 'RUNNING'" link type="danger" @click="del(scope.row.id)">删除</el-button>
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
    <el-dialog title="纪要自检详情" v-model="data.detailVisible" width="76%" top="2vh" @closed="stopStream">
      <el-descriptions v-if="data.detail.id" :column="3" border>
        <el-descriptions-item label="运行编号">{{ data.detail.run_no }}</el-descriptions-item>
        <el-descriptions-item label="会议主题">{{ data.detail.meeting_title }}</el-descriptions-item>
        <el-descriptions-item label="音视频资料">{{ data.detail.material_name || '-' }}</el-descriptions-item>
        <!-- stage 由后台每开一个步骤刷新一次，例如「第1轮：审查是否可落实」 -->
        <el-descriptions-item label="当前阶段">{{ data.detail.stage }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusTag(data.detail.status)">{{ statusText(data.detail.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="发起人">{{ data.detail.user_name }}</el-descriptions-item>
        <el-descriptions-item label="轮次">{{ data.detail.current_round }} / {{ data.detail.max_rounds }}</el-descriptions-item>
        <el-descriptions-item label="自检得分">{{ data.detail.score || '-' }}</el-descriptions-item>
        <!-- 下面三项按条件显示：处理过修订稿才有确认人，放弃过才有原因，失败了才有失败原因 -->
        <el-descriptions-item v-if="data.detail.applier_name" label="确认人">{{ data.detail.applier_name }} · {{ data.detail.applied_time }}</el-descriptions-item>
        <el-descriptions-item v-if="data.detail.reject_reason" label="放弃原因" :span="2">{{ data.detail.reject_reason }}</el-descriptions-item>
        <el-descriptions-item v-if="data.detail.error_message" label="失败原因" :span="3">{{ data.detail.error_message }}</el-descriptions-item>
      </el-descriptions>
      <div class="run-actions">
        <!-- 弹窗里的中断、恢复按钮和列表操作列调用同一组方法 -->
        <el-button v-if="data.detail.status === 'RUNNING'" type="danger" plain @click="interrupt(data.detail.id)">中断运行</el-button>
        <el-button v-if="data.detail.status === 'INTERRUPTED'" type="success" plain @click="resume(data.detail.id)">恢复运行</el-button>
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
                判定：{{ step.payload.passed ? '通过' : '不通过' }} · 得分 {{ step.payload.score }} · 问题
                {{ (step.payload.issues || []).length }} 条
              </div>
              <div v-for="(issue, index) in step.payload.issues || []" :key="index" class="issue-item">
                <!-- field 说明问题出在纪要哪个字段，issue_type 说明是哪一类问题 -->
                <el-tag size="small" type="warning">{{ fieldText(issue.field) }}</el-tag>
                <el-tag size="small" type="danger" style="margin-left: 6px">{{ issueTypeText(issue.issue_type) }}</el-tag>
                <!-- detail 是模型指出的具体问题，suggestion 是它给的改法 -->
                <div class="issue-detail">{{ issue.detail }}</div>
                <div class="issue-suggestion">建议：{{ issue.suggestion }}</div>
              </div>
            </div>
            <!-- 重写步和汇总步没有问题卡片，直接显示结论文本 -->
            <div v-else-if="step.observation_summary" class="observation">{{ step.observation_summary }}</div>
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
        <div class="revision-item"><b>会议摘要</b><div>{{ data.detail.revision.summary }}</div></div>
        <!-- 决策和待确认事项带上负责人与完成时间，模型没给出时显示未指定 -->
        <div class="revision-item">
          <b>决策</b>
          <div v-for="(item, index) in data.detail.revision.decisions || []" :key="index" class="revision-line">
            {{ index + 1 }}. {{ item.content }}（负责人：{{ item.owner_suggestion || '未指定' }}，完成时间：{{ item.deadline_suggestion || '未指定' }}）
          </div>
        </div>
        <div class="revision-item">
          <b>待确认事项</b>
          <div v-for="(item, index) in data.detail.revision.pending_items || []" :key="index" class="revision-line">
            {{ index + 1 }}. {{ item.content }}（跟进人：{{ item.owner_suggestion || '未指定' }}，完成时间：{{ item.deadline_suggestion || '未指定' }}）
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { onUnmounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'

// 事件流用 fetch 直接请求，需要自己拼后端地址，取值来自 .env.development 的 VITE_BASE_URL
const baseUrl = import.meta.env.VITE_BASE_URL
const data = reactive({
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
  detail: {},
  // 当前事件流连接的 AbortController，stopStream 用它断开
  streamController: null
})
// 六种运行状态，既做下拉选项也做表格里的中文映射
const statusOptions = [
  { label: '自检中', value: 'RUNNING' },
  { label: '等待人工确认', value: 'WAITING_CONFIRMATION' },
  { label: '已完成', value: 'SUCCEEDED' },
  { label: '已放弃修订', value: 'REJECTED' },
  { label: '自检失败', value: 'FAILED' },
  { label: '已中断', value: 'INTERRUPTED' }
]
const statusText = value => statusOptions.find(item => item.value === value)?.label || value
// 标签颜色：运行中和等待确认用橙色，放弃和中断用灰色
const statusTag = value =>
  ({
    RUNNING: 'warning',
    WAITING_CONFIRMATION: 'warning',
    SUCCEEDED: 'success',
    REJECTED: 'info',
    FAILED: 'danger',
    INTERRUPTED: 'info'
  })[value] || ''
// agent_step.step_type 的三种取值
const stepTypeText = value => ({ REVIEW: '审查', REFINE: '重写', FINAL: '汇总结论' })[value] || value
// 时间线节点和步骤标签的颜色，RUNNING 用默认的蓝色
const stepType = value => ({ SUCCEEDED: 'success', FAILED: 'danger' })[value] || 'primary'
// issue.field 指向纪要的六个内容字段，和后端 MINUTES_FIELDS 对应
const fieldText = value =>
  ({
    summary: '会议摘要',
    topics: '议题',
    viewpoints: '发言观点',
    decisions: '决策',
    pending_items: '待确认事项',
    risks: '风险与争议'
  })[value] || value
// issue.issue_type 的六种取值，和后端 REVIEW_SCHEMA 的枚举对应
const issueTypeText = value =>
  ({
    MISSING_OWNER: '缺负责人',
    MISSING_DEADLINE: '缺完成时间',
    NOT_ACTIONABLE: '无法执行',
    VAGUE: '表述含糊',
    UNSUPPORTED: '原文无依据',
    MISSING_ITEM: '纪要漏记'
  })[value] || value

const load = () =>
  request
    .get('/agent/selectPage', {
      // 四个查询参数：会议主题模糊词、状态、页码和每页条数
      params: {
        meeting_title: data.meetingTitle,
        status: data.status,
        pageNum: data.pageNum,
        pageSize: data.pageSize
      }
    })
    .then(res => {
      if (res.code === '200') {
        // list 填进表格，total 交给分页控件算页数
        data.rows = res.data?.list || []
        data.total = res.data?.total || 0
      } else ElMessage.error(res.msg)
    })

const showDetail = id =>
  request.get('/agent/selectById/' + id).then(res => {
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
const startStream = async id => {
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
      signal: controller.signal
    })
    // 权限不足等业务错误返回的是普通 JSON，不是事件流，这里当作连接失败处理
    if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream'))
      throw new Error('SSE unavailable')
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
        const line = event.split('\n').find(item => item.startsWith('data: '))
        if (line) {
          // 去掉 6 个字符的「data: 」前缀，剩下的是完整运行数据，整体替换弹窗内容
          data.detail = JSON.parse(line.slice(6))
          // 列表里这一行的进度、轮次、得分、状态一起刷新
          load()
        }
      }
    }
  } catch (error) {
    // 关弹窗、离开页面时主动 abort 也会进 catch，这种情况不提示
    if (error.name !== 'AbortError') ElMessage.error('自检实时连接已断开')
  } finally {
    // 只清理自己这条连接的控制器，不误清后面新建的连接
    if (data.streamController === controller) data.streamController = null
  }
}

const apply = approved => {
  // 覆盖和放弃共用这个请求函数，approved 区分动作，reason 是放弃原因
  const action = reason =>
    request.put('/agent/apply/' + data.detail.id, { approved, reason }).then(res => {
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
    ElMessageBox.confirm('修订稿会覆盖当前纪要内容，原内容不再保留，确定应用吗？', '人工确认', { type: 'warning' }).then(() =>
      action(null)
    )
  else
    // 放弃前弹输入框收集原因，点确定以 approved=false 和输入值提交
    ElMessageBox.prompt('请填写放弃原因', '放弃修订', { inputPlaceholder: '例如：修订内容与实际讨论不符' }).then(
      ({ value }) => action(value)
    )
}

const interrupt = id =>
  request.put('/agent/interrupt/' + id).then(res => {
    if (res.code === '200') {
      ElMessage.success('自检已中断')
      // 运行不会再变化，先断开事件流，再读取一次详情显示「已中断」
      stopStream()
      showDetail(id)
      load()
    } else ElMessage.error(res.msg)
  })

const resume = id =>
  request.put('/agent/resume/' + id).then(res => {
    if (res.code === '200') {
      ElMessage.success('自检已恢复运行')
      // 状态已改回 RUNNING，showDetail 会重新建立事件流
      showDetail(id)
      load()
    } else ElMessage.error(res.msg)
  })

const del = id =>
  ElMessageBox.confirm('删除后这次自检的全部步骤记录都会消失，确定删除吗？', '删除确认', { type: 'warning' }).then(() =>
    request.delete('/agent/delete/' + id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 重新拉一次列表，被删的那行消失
        load()
      } else ElMessage.error(res.msg)
    })
  )

// 反思环是后台异步跑的，列表里只要还有在跑的运行就每三秒重拉一次，进度条自己往前走
const timer = window.setInterval(() => {
  if (data.rows.some(item => item.status === 'RUNNING')) load()
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
  color: #909399;
  line-height: 1.7;
}
.issue-count {
  color: #f56c6c;
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
  background: #f7f8fa;
  border-radius: 5px;
}
.step-title {
  font-weight: bold;
}
.elapsed {
  float: right;
  color: #909399;
  font-weight: normal;
}
.summary {
  margin-top: 8px;
}
.observation {
  margin-top: 8px;
  white-space: pre-wrap;
  color: #606266;
}
.review-body {
  margin-top: 10px;
}
.review-head {
  color: #606266;
  margin-bottom: 8px;
}
.issue-item {
  padding: 9px 10px;
  margin-bottom: 8px;
  background: white;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}
.issue-detail {
  margin-top: 6px;
}
.issue-suggestion {
  margin-top: 4px;
  color: #909399;
}
.final-result {
  padding: 15px;
  background: #f0f9eb;
  border-left: 4px solid #67c23a;
  white-space: pre-wrap;
  line-height: 1.8;
}
.revision {
  margin-top: 14px;
  padding: 15px;
  background: #fafbfc;
  border: 1px solid #ebeef5;
  border-radius: 5px;
}
.revision h3 {
  margin: 0 0 10px;
}
.revision-item {
  margin-bottom: 12px;
  line-height: 1.8;
}
.revision-line {
  color: #606266;
}
.revision-title {
  margin-left: 8px;
  font-weight: bold;
}
</style>
