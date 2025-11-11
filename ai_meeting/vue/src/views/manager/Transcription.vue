<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议下拉框，可清空表示不限会议 -->
      <el-select v-model="data.meetingId" clearable filterable style="width: 330px; margin-right: 10px" placeholder="请选择会议">
        <el-option v-for="item in data.meetings" :key="item.id" :label="meetingLabel(item)" :value="item.id"></el-option>
      </el-select>
      <!-- 资料文件名输入框，后端按它反查资料ID再过滤任务 -->
      <el-input v-model="data.fileName" style="width: 210px; margin-right: 10px" placeholder="请输入资料文件名"></el-input>
      <!-- 任务状态下拉框 -->
      <el-select v-model="data.status" clearable style="width: 140px; margin-right: 10px" placeholder="任务状态">
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value"></el-option>
      </el-select>
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>

    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="meeting_title" label="会议主题" min-width="180" show-overflow-tooltip />
        <el-table-column prop="material_name" label="音视频资料" min-width="210" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="scope"><el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="进度" width="150">
          <template #default="scope">
            <!-- 失败时进度条变红，完成时变绿，执行中保持默认蓝色 -->
            <el-progress :percentage="scope.row.progress" :stroke-width="10" :status="scope.row.status === 'FAILED' ? 'exception' : (scope.row.status === 'SUCCEEDED' ? 'success' : '')" />
          </template>
        </el-table-column>
        <el-table-column prop="segment_count" label="片段数" width="80" />
        <el-table-column prop="total_duration_text" label="时长" width="100" />
        <el-table-column prop="initiator_name" label="发起人" width="100" />
        <el-table-column prop="retry_count" label="重试" width="70" />
        <el-table-column prop="create_time" label="提交时间" width="175" />
        <el-table-column label="操作" width="250" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="showDetail(scope.row.id)">详情</el-button>
            <!-- 转写完成后才能生成纪要，跳到第 11 章的功能 -->
            <el-button v-if="scope.row.status === 'SUCCEEDED'" link type="success" @click="generateMinutes(scope.row)">生成纪要</el-button>
            <!-- 失败且没到重试上限时出现重试按钮 -->
            <el-button v-if="scope.row.status === 'FAILED' && scope.row.retry_count < scope.row.max_retry" link type="warning" @click="retry(scope.row)">重试</el-button>
            <!-- 卡死任务（长时间 PENDING/PROCESSING 且后台协程已丢失）的出路 -->
            <el-button v-if="['PENDING', 'PROCESSING'].includes(scope.row.status) && stuckOverThreshold(scope.row)" link type="danger" @click="forceFail(scope.row)">强制结束</el-button>
            <!-- 执行中的任务不能删，删除时由后端连带处理这个转写生成的纪要 -->
            <el-button v-if="!['PENDING', 'PROCESSING'].includes(scope.row.status)" link type="danger" @click="del(scope.row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
    <div class="card" v-if="data.total">
      <el-pagination v-model:current-page="data.pageNum" background layout="prev, pager, next" :page-size="data.pageSize" :total="data.total" @current-change="load" />
    </div>

    <el-dialog title="转写详情" v-model="data.detailVisible" width="72%" top="4vh">
      <el-descriptions v-if="data.detail.id" :column="3" border>
        <el-descriptions-item label="会议主题">{{ data.detail.meeting_title }}</el-descriptions-item>
        <el-descriptions-item label="资料文件">{{ data.detail.material_name }}</el-descriptions-item>
        <el-descriptions-item label="转写模型">{{ data.detail.model_name }}</el-descriptions-item>
        <el-descriptions-item label="状态"><el-tag :type="statusTag(data.detail.status)">{{ statusText(data.detail.status) }}</el-tag></el-descriptions-item>
        <el-descriptions-item label="进度">{{ data.detail.progress }}% / {{ data.detail.stage }}</el-descriptions-item>
        <el-descriptions-item label="音频时长">{{ data.detail.total_duration_text }}</el-descriptions-item>
        <!-- 只有失败任务才有失败原因，这一行才渲染 -->
        <el-descriptions-item v-if="data.detail.error_message" label="失败原因" :span="3">{{ data.detail.error_message }}</el-descriptions-item>
      </el-descriptions>

      <template v-if="data.detail.status === 'SUCCEEDED'">
        <div class="section-title">
          说话人与参会人员匹配
          <!-- loading 期间按钮转圈，防止连点发起多次模型调用 -->
          <el-button size="small" type="primary" plain :loading="data.suggesting" @click="suggestSpeakers">AI建议匹配</el-button>
          <span class="section-tip">模型只能听出哪几段是同一个人，认不出是谁，所以由它读发言内容推荐，你确认后再保存</span>
        </div>
        <el-table :data="data.detail.speakers || []" size="small" border>
          <!-- 模型给的标签，形如 01-0；前缀 01 是第 8 章按音频分段序号加的 -->
          <el-table-column prop="speaker_label" label="说话人标签" width="130" />
          <!-- 该标签下有多少条片段，由后端汇总时累加得到 -->
          <el-table-column prop="segment_count" label="发言片段数" width="120" />
          <el-table-column label="匹配参会人员" min-width="260">
            <template #default="scope">
              <!-- 下拉框直接绑定这一行的 speaker_user_id，选中或清空都触发 matchSpeaker -->
              <!-- clearable 让用户能清空回到未匹配状态 -->
              <el-select v-model="scope.row.speaker_user_id" clearable filterable style="width: 100%" placeholder="请选择参会人员" @change="matchSpeaker(scope.row)">
                <el-option v-for="item in data.detail.participants || []" :key="item.id" :label="participantLabel(item)" :value="item.id"></el-option>
              </el-select>
            </template>
          </el-table-column>
          <!-- AI 建议列，data.suggestions 是以说话人标签为键的对象，没建议的标签显示“暂无建议” -->
          <el-table-column label="AI建议" min-width="300">
            <template #default="scope">
              <div v-if="data.suggestions[scope.row.speaker_label]" class="suggestion">
                <el-tag size="small" type="success">{{ data.suggestions[scope.row.speaker_label].user_name }}</el-tag>
                <el-tag size="small" style="margin-left: 6px">{{ data.suggestions[scope.row.speaker_label].confidence_text }}</el-tag>
                <el-button link type="primary" style="margin-left: 8px" @click="acceptSuggestion(scope.row)">采纳</el-button>
                <!-- 模型给的判断依据，采纳前可以对照时间轴里的发言内容核对 -->
                <div class="suggestion-reason">{{ data.suggestions[scope.row.speaker_label].reason }}</div>
              </div>
              <span v-else class="suggestion-empty">暂无建议</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="section-title">时间轴转写片段</div>
        <!-- max-height 让片段很多时表格内部滚动，弹窗本身不会被撑得过长 -->
        <el-table :data="data.detail.segments || []" max-height="430" border>
          <el-table-column prop="segment_no" label="序号" width="72" align="center" />
          <!-- time_range 由后端把起止毫秒拼成 00:00:12 - 00:00:20 -->
          <el-table-column prop="time_range" label="时间" width="170" />
          <el-table-column label="说话人" width="130">
            <template #default="scope">{{ scope.row.speaker_name || ('说话人 ' + scope.row.speaker_label) }}</template>
          </el-table-column>
          <!-- 显示的是可修订的 content，不是模型原始文本 -->
          <el-table-column prop="content" label="转写文本" min-width="360" />
          <el-table-column label="操作" width="80">
            <template #default="scope"><el-button link type="primary" @click="editSegment(scope.row)">修订</el-button></template>
          </el-table-column>
        </el-table>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onUnmounted, reactive } from 'vue'
import { useRoute } from 'vue-router'
import router from '@/router/index.js'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'
import { TRANSCRIPTION_STATUS } from '@/constants/status.js'
import { meetingLabel } from '@/constants/label.js'

const route = useRoute()

const data = reactive({
  // 顶部会议下拉框的值；从第 6 章提交转写后跳过来时，初值取路由 query.meetingId
  meetingId: Number(route.query.meetingId) || null,
  // 会议下拉框的选项，由 loadMeetings 请求 /meeting/selectOptions 填充
  meetings: [],
  // 资料文件名输入框，作为 selectPage 的 fileName 参数
  fileName: '',
  // 任务状态下拉框，作为 selectPage 的 status 参数
  status: '',
  // 当前页码，查询和重置时回到 1
  pageNum: 1,
  // 每页条数，随 selectPage 请求提交
  pageSize: 10,
  // selectPage 返回的总条数，为 0 时分页条不渲染
  total: 0,
  // selectPage 返回的当前页任务，表格逐行渲染，定时器也按它判断是否还有未完成任务
  tableData: [],
  // 控制转写详情弹窗显示
  detailVisible: false,
  // selectById 返回的任务详情，弹窗读取任务信息、片段和参会人员
  detail: {},
  // “AI建议匹配”按钮的 loading 状态，请求发出时置 true，收到响应或请求失败后置 false
  suggesting: false,
  // 以说话人标签为键保存模型给出的建议，表格“AI建议”列按 scope.row.speaker_label 取值
  suggestions: {}
})

// 状态字典唯一真源在 @/constants（此前五页各自复制，已漂移）
const { options: statusOptions, text: statusText, tag: statusTag } = TRANSCRIPTION_STATUS
// 下拉框每个选项的文字：姓名 / 部门 / 职位，数据来自详情接口的 participants
const participantLabel = (item) => [item.name, item.department_name, item.position].filter(Boolean).join(' / ')

const loadMeetings = () => {
  request.get('/meeting/selectOptions').then(res => {
    if (res.code === '200') {
      data.meetings = res.data || []
      // 从第 6 章带过来的会议ID如果不在可见范围里，退回不限会议
      if (data.meetingId && !data.meetings.some(item => item.id === data.meetingId)) {
        data.meetingId = null
      }
      load()
    } else {
      ElMessage.error(res.msg)
    }
  })
}


// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.tableData || data.tableData.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () => {
  request.get('/transcription/selectPage', {
    params: {
      // 为 null 时转成 undefined，Axios 不拼这个参数，后端按不限会议处理
      meetingId: data.meetingId || undefined,
      status: data.status,
      fileName: data.fileName,
      pageNum: data.pageNum,
      pageSize: data.pageSize
    }
  }).then(res => {
    if (res.code === '200') {
      data.tableData = res.data?.list || []
      data.total = res.data?.total || 0
      // 详情弹窗开着且里面这个任务还没跑完时，顺带把详情也刷新一次
      if (data.detailVisible && data.detail.id && ['PENDING', 'PROCESSING'].includes(data.detail.status)) {
        showDetail(data.detail.id)
      }
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const showDetail = (id) => {
  // 点击“详情”、匹配说话人或修订片段后都会调用，任务ID拼进地址 GET /transcription/selectById/{task_id}
  request.get('/transcription/selectById/' + id).then(res => {
    if (res.code === '200') {
      // 换一条任务就把上一条的 AI 建议清掉，避免标签串到别的任务上
      if (data.detail.id !== res.data.id) data.suggestions = {}
      // 整个任务对象赋给 data.detail，弹窗里的任务信息、说话人表格、时间轴表格都从它取数
      data.detail = res.data
      data.detailVisible = true
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const del = (row) => {
  // row 是点击的任务行，material_name 用于确认框里提示是哪份资料的转写
  ElMessageBox.confirm(
      `确定删除“${row.material_name}”的转写任务吗？转写出的时间轴分段会一并删除，由它生成的会议纪要也会被删除。`,
      '删除转写任务',
      { type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消' }
  ).then(() => {
    // 点“确定删除”后才发 DELETE /transcription/delete/{task_id}；点“取消”时不发请求
    request.delete('/transcription/delete/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 重新拉当前页，被删的任务从表格消失
        reloadAfterDelete()
      } else {
        // 例如“只有管理员或转写发起人可以删除”“该转写的纪要正在生成中，请等生成结束后再删除”
        ElMessage.error(res.msg)
      }
    })
  })
}

// 卡死判定：非终态且 20 分钟没有任何进度变化（update_time 由每次进度写库刷新）
const STUCK_MINUTES = 20
const stuckOverThreshold = row => {
  if (!['PENDING', 'PROCESSING'].includes(row.status)) return false
  if (!row.update_time) return true
  return Date.now() - new Date(row.update_time.replace(' ', 'T')).getTime() > STUCK_MINUTES * 60 * 1000
}

const forceFail = row => {
  ElMessageBox.confirm(
      `任务 ${row.task_no} 已 ${STUCK_MINUTES} 分钟无进展，确定强制结束吗？结束后可点击重试重新排队。`,
      '强制结束', { type: 'warning' }
  ).then(() => {
    request.put('/transcription/forceFail/' + row.id).then(res => {
      if (res.code === '200') { ElMessage.success('已强制结束'); load() }
    })
  }).catch(() => {})
}

const retry = (row) => {
  ElMessageBox.confirm('确定重试该转写任务吗？', '重试确认', { type: 'warning' }).then(() => {
    // 任务ID拼在地址里，不需要请求体
    request.put('/transcription/retry/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('转写任务已重新提交')
        // 重新拉列表，这一行状态回到“等待执行”，进度归零
        load()
      } else {
        ElMessage.error(res.msg)
      }
    })
  })
}

const generateMinutes = (row) => {
  // 一个转写任务只保留一份纪要，重新生成会连同确认状态一起覆盖，所以先提醒
  ElMessageBox.confirm(`确定根据${row.meeting_title}的转写内容生成会议纪要吗？这份音视频资料如果已经生成过纪要，原有内容会被覆盖。`, '生成纪要', { type: 'warning' }).then(() => {
    // 转写任务ID拼在地址里，后端按它读取这个任务的转写片段作为模型输入
    request.post('/meetingMinutes/generate/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('会议纪要已开始生成')
        // 生成是异步的，跳到纪要页并带上会议ID，在那边看进度条
        router.push({ path: '/manager/meetingMinutes', query: { meetingId: row.meeting_id } })
      } else ElMessage.error(res.msg)
    })
  })
}

// 让模型读一段转写猜谁是谁，结果只填到页面上，人点了采纳才会真正保存
const suggestSpeakers = () => {
  // 打开建议匹配按钮的 loading，收到响应后关闭加载状态
  data.suggesting = true
  request.post('/transcription/suggestSpeakers/' + data.detail.id).then(res => {
    data.suggesting = false
    if (res.code === '200') {
      // 后端返回的是数组，这里转成以说话人标签为键的对象，表格每一行按 speaker_label 取自己的建议
      const suggestions = {}
      ;(res.data || []).forEach(item => { suggestions[item.speaker_label] = item })
      data.suggestions = suggestions
      ElMessage.success(`已给出 ${res.data?.length || 0} 条建议，确认无误后点采纳`)
    } else ElMessage.error(res.msg)
  }).catch(() => { data.suggesting = false })
}

// “采纳”按钮的回调，speaker 是说话人表格的这一行
const acceptSuggestion = (speaker) => {
  // 按这一行的标签取出模型给的建议，没有建议时不做任何事
  const suggestion = data.suggestions[speaker.speaker_label]
  if (!suggestion) return
  // 把建议的用户ID填进这一行的下拉框，再走和手动选人完全一样的保存流程
  speaker.speaker_user_id = suggestion.user_id
  matchSpeaker(speaker)
}

const matchSpeaker = (speaker) => {
  request.put('/transcription/matchSpeaker', {
    // 当前打开的转写任务ID
    task_id: data.detail.id,
    // 这一行的说话人标签，后端按它圈定要更新哪些片段
    speaker_label: speaker.speaker_label,
    // 下拉框被清空时值是空串，转成 null 表示解除匹配
    user_id: speaker.speaker_user_id || null
  }).then(res => {
    if (res.code === '200') {
      ElMessage.success('说话人匹配已保存')
      // 重新拉详情，时间轴上这个标签的所有片段都换成新姓名
      showDetail(data.detail.id)
    } else {
      ElMessage.error(res.msg)
      // 失败时也重拉一次，把下拉框恢复成数据库里的真实值
      showDetail(data.detail.id)
    }
  })
}

const editSegment = (segment) => {
  // 用带多行输入框的确认框收集修订后的文本
  ElMessageBox.prompt('请修订转写文本', '修订时间轴片段', {
    inputType: 'textarea',
    // 初始值是这条片段当前的 content，用户在原文基础上改
    inputValue: segment.content,
    // 至少要有一个非空白字符，和后端的非空校验口径一致
    inputPattern: /\S+/,
    inputErrorMessage: '转写文本不能为空'
  }).then(({ value }) => {
    // 提交片段ID和新文本，后端按ID定位这一条
    request.put('/transcription/updateSegment', { id: segment.id, content: value }).then(res => {
      if (res.code === '200') {
        ElMessage.success('转写文本已保存')
        // 重新拉详情，这一行的文本随之更新
        showDetail(data.detail.id)
      } else {
        ElMessage.error(res.msg)
      }
    })
  })
}

const search = () => {
  data.pageNum = 1
  load()
}

const reset = () => {
  // 三个条件全部清空，包括会议下拉框
  data.meetingId = null
  data.fileName = ''
  data.status = ''
  data.pageNum = 1
  load()
}

const timer = window.setInterval(() => {
  // 每 3 秒检查一次：当前页里还有等待执行或转写中的任务时才请求 selectPage，全部结束后不再发请求
  if (data.tableData.some(item => ['PENDING', 'PROCESSING'].includes(item.status))) {
    load()
  }
}, 3000)

// 离开页面时清掉定时器，避免组件销毁后还在发请求
onUnmounted(() => window.clearInterval(timer))
loadMeetings()
</script>

<style scoped>
.section-title {
  font-weight: bold;
  margin: 20px 0 10px;
}
.section-tip {
  margin-left: 10px;
  font-weight: normal;
  color: rgba(17, 24, 39, .55);
}
.suggestion-reason {
  margin-top: 4px;
  color: rgba(17, 24, 39, .55);
  line-height: 1.6;
}
.suggestion-empty {
  color: rgba(17, 24, 39, .35);
}
</style>
