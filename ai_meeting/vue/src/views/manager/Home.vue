<template>
  <div>
    <!-- 欢迎区：登录用户姓名、当天日期和去会议管理的按钮 -->
    <div class="card home-welcome">
      <div>
        <div class="home-hello">您好，{{ data.user?.name }}</div>
        <div class="home-sub">{{ today }}</div>
      </div>
      <el-button type="primary" plain @click="router.push('/manager/meeting')">去会议管理</el-button>
    </div>

    <!-- 四个数字来自各列表接口的分页总数，管理员看全公司，员工看自己相关的 -->
    <div class="home-metrics">
      <div class="card home-metric" v-for="item in metrics" :key="item.label" @click="router.push(item.path)">
        <div class="home-metric-label">{{ item.label }}</div>
        <div class="home-metric-value">{{ item.value }}<span class="home-metric-unit">{{ item.unit }}</span></div>
        <div class="home-metric-hint">{{ item.hint }}</div>
      </div>
    </div>

    <div class="home-columns">
      <!-- 左边是最近的会议，按开始时间倒序取五条 -->
      <div class="card home-panel">
        <div class="home-panel-head">
          <span>最近会议</span>
          <a @click="router.push('/manager/meeting')">全部</a>
        </div>
        <!-- 没有会议时显示空状态，有会议时每行显示主题、开始时间、主持人、参会人数和状态 -->
        <el-empty v-if="!data.meetings.length" description="还没有会议记录" :image-size="70" />
        <div v-else>
          <div class="home-row" v-for="item in data.meetings" :key="item.id">
            <div class="home-row-main">
              <div class="home-row-title">{{ item.title }}</div>
              <div class="home-row-sub">{{ item.start_time }}·{{ item.host_name }}主持·{{ item.participant_count }}人参会</div>
            </div>
            <el-tag size="small" :type="meetingTag(item.status)">{{ meetingText(item.status) }}</el-tag>
          </div>
        </div>
      </div>

      <!-- 右边是最近的会议纪要，按生成时间倒序取五条 -->
      <div class="card home-panel">
        <div class="home-panel-head">
          <span>最近纪要</span>
          <a @click="router.push('/manager/meetingMinutes')">全部</a>
        </div>
        <!-- 没有纪要时显示空状态，有纪要时每行显示会议主题、音视频资料名、生成时间和状态 -->
        <el-empty v-if="!data.minutes.length" description="还没有会议纪要" :image-size="70" />
        <div v-else>
          <div class="home-row" v-for="item in data.minutes" :key="item.id">
            <div class="home-row-main">
              <div class="home-row-title">{{ item.meeting_title }}</div>
              <div class="home-row-sub">{{ item.material_name || '会议素材' }}·{{ item.create_time }}</div>
            </div>
            <el-tag size="small" :type="minutesTag(item.status)">{{ minutesText(item.status) }}</el-tag>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive } from 'vue'
import request from '@/utils/request.js'
import { MEETING_STATUS, MINUTES_STATUS } from '@/constants/status.js'
import router from '@/router/index.js'

const data = reactive({
  // 登录时存进 localStorage 的用户信息，欢迎语显示其中的 name
  user: JSON.parse(localStorage.getItem('xm-user') || '{}'),
  // 四张卡片的数字，confirmedMinutes 用在「会议纪要」卡片的说明文字里
  counts: { meeting: 0, transcription: 0, minutes: 0, confirmedMinutes: 0, agentRun: 0 },
  // 「最近会议」和「最近纪要」两个面板的列表
  meetings: [],
  minutes: []
})

// 欢迎语下面的日期，按浏览器当前时间拼成「年月日 星期」
const today = computed(() => {
  const now = new Date()
  const week = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'][now.getDay()]
  return `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日 ${week}`
})

// 四张统计卡片，点一下跳到对应的功能页。
// 数字都取各列表接口分页返回的 total，不额外做一套统计接口
const metrics = computed(() => [
  { label: '我的会议', value: data.counts.meeting, unit: '场', hint: '我创建、主持或参与的会议', path: '/manager/meeting' },
  { label: '转写任务', value: data.counts.transcription, unit: '个', hint: '已上传并发起转写的音视频', path: '/manager/transcription' },
  { label: '会议纪要', value: data.counts.minutes, unit: '份', hint: `其中 ${data.counts.confirmedMinutes} 份已确认`, path: '/manager/meetingMinutes' },
  { label: '纪要自检', value: data.counts.agentRun, unit: '次', hint: 'Agent 跑过的自检运行', path: '/manager/agent' }
])

// 会议状态和纪要状态的中文与标签颜色：共享 @/constants（与各管理页同源，
// 此前本页 SCHEDULED/CANCELLED 的文案颜色与会议管理页不一致，已统一到管理页版本）
const meetingText = MEETING_STATUS.text
const meetingTag = MEETING_STATUS.tag
const minutesText = MINUTES_STATUS.text
const minutesTag = MINUTES_STATUS.tag

// 卡片上的数字全部来自各列表接口的分页总数。
// pageSize 传 1 是因为这里只要 total，不需要真的把列表拉回来
const countOf = (url, params) =>
  request.get(url, { params }).then(res => (res.code === '200' ? res.data?.total || 0 : 0))

// 会议接口的分页参数是下划线写法，其余三个是驼峰，这里按各自的来
const loadCounts = () => {
  countOf('/meeting/selectPage', { view_type: 'MY', page_num: 1, page_size: 1 }).then(value => (data.counts.meeting = value))
  countOf('/transcription/selectPage', { pageNum: 1, pageSize: 1 }).then(value => (data.counts.transcription = value))
  countOf('/meetingMinutes/selectPage', { pageNum: 1, pageSize: 1 }).then(value => (data.counts.minutes = value))
  countOf('/meetingMinutes/selectPage', { status: 'CONFIRMED', pageNum: 1, pageSize: 1 }).then(
    value => (data.counts.confirmedMinutes = value)
  )
  countOf('/agent/selectPage', { pageNum: 1, pageSize: 1 }).then(value => (data.counts.agentRun = value))
}

// 最近会议：view_type 用 MY，管理员和员工都只看与自己有关的会议
const loadMeetings = () => {
  request.get('/meeting/selectPage', {
    params: { view_type: 'MY', page_num: 1, page_size: 5 }
  }).then(res => {
    if (res.code === '200') {
      // 前五条会议填进「最近会议」面板，空数组时显示空状态
      data.meetings = res.data?.list || []
    }
  })
}

// 最近纪要：接口已经按当前身份过滤过，这里只取前五条
const loadMinutes = () => {
  request.get('/meetingMinutes/selectPage', {
    params: { pageNum: 1, pageSize: 5 }
  }).then(res => {
    if (res.code === '200') {
      // 前五条纪要填进「最近纪要」面板，空数组时显示空状态
      data.minutes = res.data?.list || []
    }
  })
}

// 页面打开时三组请求同时发出，各自返回后更新自己的区域
loadCounts()
loadMeetings()
loadMinutes()
</script>

<style scoped>
.home-welcome {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.home-hello {
  font-size: 18px;
  font-weight: 600;
  color: rgba(17, 24, 39, .95);
}

.home-sub {
  margin-top: 6px;
  font-size: 13px;
  color: rgba(17, 24, 39, .55);
}

/* 四张统计卡片一行排开，窄屏时自动折行 */
.home-metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 12px;
}

.home-metric {
  cursor: pointer;
  transition: border-color .2s;
}

.home-metric:hover {
  border-color: #2563eb;
}

.home-metric-label {
  font-size: 13px;
  color: rgba(17, 24, 39, .55);
}

.home-metric-value {
  margin: 8px 0 4px;
  font-size: 26px;
  font-weight: 600;
  color: rgba(17, 24, 39, .95);
}

.home-metric-unit {
  margin-left: 4px;
  font-size: 13px;
  font-weight: normal;
  color: rgba(17, 24, 39, .55);
}

.home-metric-hint {
  font-size: 12px;
  color: rgba(17, 24, 39, .55);
}

.home-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.home-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 15px;
  font-weight: 600;
  color: rgba(17, 24, 39, .95);
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(17, 24, 39, .10);
}

.home-panel-head a {
  font-size: 13px;
  font-weight: normal;
  color: #2563eb;
  cursor: pointer;
}

.home-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 0;
  border-bottom: 1px solid #f1f5f9;
}

.home-row:last-child {
  border-bottom: none;
}

.home-row-main {
  min-width: 0;
  padding-right: 10px;
}

.home-row-title {
  font-size: 14px;
  color: rgba(17, 24, 39, .95);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.home-row-sub {
  margin-top: 4px;
  font-size: 12px;
  color: rgba(17, 24, 39, .55);
}

@media (max-width: 1100px) {
  .home-metrics {
    grid-template-columns: repeat(2, 1fr);
  }

  .home-columns {
    grid-template-columns: 1fr;
  }
}
</style>
