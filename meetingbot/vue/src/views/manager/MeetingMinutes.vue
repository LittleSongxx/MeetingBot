<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议下拉框，写入 data.meetingId；选项文字是“会议编号 / 主题”，clearable 清空后不限会议 -->
      <el-select
        v-model="data.meetingId"
        clearable
        filterable
        style="width: 340px; margin-right: 10px"
        placeholder="请选择会议"
      >
        <el-option
          v-for="item in data.meetings"
          :key="item.id"
          :label="`${item.meeting_no} / ${item.title}`"
          :value="item.id"
        ></el-option>
      </el-select>
      <!-- 纪要状态下拉框，写入 data.status，选项来自 statusOptions -->
      <el-select v-model="data.status" clearable style="width: 150px; margin-right: 10px" placeholder="纪要状态">
        <el-option v-for="item in statusOptions" :key="item.value" :label="item.label" :value="item.value"></el-option>
      </el-select>
      <!-- “查询”回到第一页再请求；“重置”清空会议和状态两个条件 -->
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="meeting_title" label="会议主题" min-width="180" show-overflow-tooltip />
        <!-- material_name 是 minutes_dict 顺着来源转写任务查出来的音视频文件名 -->
        <el-table-column prop="material_name" label="音视频资料" min-width="200" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="scope"
            ><el-tag :type="statusTag(scope.row.status)">{{ statusText(scope.row.status) }}</el-tag></template
          >
        </el-table-column>
        <!-- 进度条读 progress：失败时红色，DRAFT 和 CONFIRMED 绿色，生成中是默认蓝色 -->
        <el-table-column label="生成进度" width="190">
          <template #default="scope">
            <el-progress
              :percentage="scope.row.progress"
              :status="
                scope.row.status === 'FAILED'
                  ? 'exception'
                  : ['DRAFT', 'CONFIRMED'].includes(scope.row.status)
                    ? 'success'
                    : ''
              "
            />
          </template>
        </el-table-column>
        <!-- 生成这份纪要用的模型名，来自 ai_model_config.model_name -->
        <el-table-column prop="model_name" label="生成模型" min-width="160" />
        <el-table-column prop="creator_name" label="创建人" width="100" />
        <el-table-column prop="create_time" label="创建时间" width="175" />
        <el-table-column label="操作" width="540" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="showDetail(scope.row.id)">详情</el-button>
            <!-- 只有草稿能改，已确认的纪要要保持内容稳定 -->
            <el-button v-if="scope.row.status === 'DRAFT'" link type="primary" @click="openEdit(scope.row.id)"
              >修订</el-button
            >
            <el-button v-if="scope.row.status === 'DRAFT'" link type="success" @click="confirmMinutes(scope.row)"
              >确认</el-button
            >
            <!-- 自检只对已生成的纪要有意义，生成中和失败的没有内容可查 -->
            <el-button
              v-if="['DRAFT', 'CONFIRMED'].includes(scope.row.status)"
              link
              type="warning"
              @click="startReview(scope.row)"
              >Agent自检</el-button
            >
            <el-button v-if="scope.row.status === 'FAILED'" link type="warning" @click="retry(scope.row)"
              >重试</el-button
            >
            <!-- 已确认的纪要要改内容，先退回草稿 -->
            <el-button v-if="scope.row.status === 'CONFIRMED'" link type="warning" @click="unconfirmMinutes(scope.row)"
              >取消确认</el-button
            >
            <!-- 已确认纪要可以直接把待确认事项转成行动项草稿 -->
            <el-button v-if="scope.row.status === 'CONFIRMED'" link type="primary" @click="generateFromRow(scope.row)"
              >生成待办</el-button
            >
            <!-- 草稿和已确认都有内容可导，生成中和失败的没有 -->
            <template v-if="['DRAFT', 'CONFIRMED'].includes(scope.row.status)">
              <el-button link type="primary" @click="download(scope.row, 'word')">Word</el-button>
              <el-button link type="primary" @click="download(scope.row, 'pdf')">PDF</el-button>
            </template>
            <!-- 生成中的纪要不能删，删完要重新从转写任务生成 -->
            <el-button v-if="scope.row.status !== 'GENERATING'" link type="danger" @click="del(scope.row)"
              >删除</el-button
            >
          </template>
        </el-table-column>
      </el-table>
    </div>
    <!-- data.total 为 0 时整个分页条不渲染；点页码时 v-model 写回 data.pageNum，再触发 load -->
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

    <el-dialog
      :title="`${data.detail.meeting_title || ''} 会议纪要`"
      v-model="data.detailVisible"
      width="76%"
      top="3vh"
    >
      <!-- 顶部工具栏：问答入口，只有草稿/已确认纪要有内容可问 -->
      <div v-if="['DRAFT', 'CONFIRMED'].includes(data.detail.status)" class="minutes-toolbar">
        <el-button type="primary" plain size="small" @click="openQa">问答</el-button>
        <span class="minutes-toolbar-tip">基于转写原文回答，答案会附原文引用</span>
      </div>
      <el-descriptions v-if="data.detail.id" :column="3" border>
        <el-descriptions-item label="状态"
          ><el-tag :type="statusTag(data.detail.status)">{{
            statusText(data.detail.status)
          }}</el-tag></el-descriptions-item
        >
        <el-descriptions-item label="创建人">{{ data.detail.creator_name }}</el-descriptions-item>
        <!-- 还没确认时 confirmer_name 是 null，显示成短横线 -->
        <el-descriptions-item label="确认人">{{ data.detail.confirmer_name || '-' }}</el-descriptions-item>
        <!-- 顺着 source_task_id 反查出来的音视频文件名，说明这份纪要是从哪个文件来的 -->
        <el-descriptions-item label="音视频资料" :span="2">{{ data.detail.material_name || '-' }}</el-descriptions-item>
        <el-descriptions-item label="生成模型">{{ data.detail.model_name || '-' }}</el-descriptions-item>
        <!-- 只有生成失败的纪要才有这一项 -->
        <el-descriptions-item v-if="data.detail.error_message" label="失败原因" :span="3">{{
          data.detail.error_message
        }}</el-descriptions-item>
      </el-descriptions>
      <!-- 生成中和失败的纪要没有内容可展示，这块整体不渲染 -->
      <div v-if="['DRAFT', 'CONFIRMED'].includes(data.detail.status)" class="minutes-content">
        <h3>会议摘要</h3>
        <p>{{ data.detail.summary }}</p>
        <!-- 五块数组内容结构一样，都是「一组条目、每条若干字段」，所以抽成一个渲染组件 -->
        <minutes-section
          title="议题总结"
          :items="data.detail.topics"
          :fields="[
            ['title', '议题'],
            ['summary', '总结']
          ]"
        />
        <minutes-section
          title="发言观点"
          :items="data.detail.viewpoints"
          :fields="[
            ['speaker', '发言人'],
            ['viewpoint', '观点']
          ]"
        />
        <minutes-section
          title="会议决策"
          :items="data.detail.decisions"
          :fields="[
            ['content', '决策'],
            ['basis', '依据'],
            ['owner_suggestion', '建议负责人'],
            ['deadline_suggestion', '建议期限']
          ]"
        />
        <minutes-section
          title="待确认事项"
          :items="data.detail.pending_items"
          :fields="[
            ['content', '事项'],
            ['owner_suggestion', '建议负责人'],
            ['deadline_suggestion', '建议期限']
          ]"
        />
        <minutes-section
          title="风险与争议点"
          :items="data.detail.risks"
          :fields="[
            ['content', '内容'],
            ['level', '级别'],
            ['suggestion', '建议']
          ]"
        />
      </div>
      <!-- 详情底部行动项区块：草稿可确认生效、待办可完成；已确认纪要可一键生成待办 -->
      <div v-if="['DRAFT', 'CONFIRMED'].includes(data.detail.status)" class="minutes-actions">
        <div class="minutes-actions-head">
          <h3>行动项</h3>
          <div class="minutes-actions-tools">
            <el-button
              v-if="data.detail.status === 'CONFIRMED'"
              link
              type="primary"
              :loading="data.generatingActions"
              @click="generateActions"
              >生成待办</el-button
            >
            <a class="minutes-actions-link" @click="router.push('/manager/actionItems')">前往行动项</a>
          </div>
        </div>
        <el-empty v-if="!data.actionItems.length" description="还没有行动项" :image-size="60" />
        <div v-else class="minutes-action-list">
          <div v-for="item in data.actionItems" :key="item.id" class="minutes-action">
            <div class="minutes-action-main">
              <div class="minutes-action-content">{{ item.content }}</div>
              <div class="minutes-action-meta">
                <el-tag size="small" :type="actionStatusTag(item.status)">{{ actionStatusText(item.status) }}</el-tag>
                <span v-if="sourceOfAction(item) === 'AI'" class="minutes-action-ai">AI</span>
                <span>负责人：{{ actionOwner(item) }}</span>
                <span>截止：{{ actionDue(item) }}</span>
              </div>
            </div>
            <div class="minutes-action-ops">
              <el-button
                v-if="item.status === 'DRAFT'"
                link
                type="primary"
                :loading="data.confirmingActionId === item.id"
                @click="confirmActionItem(item)"
                >确认生效</el-button
              >
              <el-button v-if="item.status === 'OPEN'" link type="success" @click="completeActionItem(item)">完成</el-button>
            </div>
          </div>
        </div>
      </div>
    </el-dialog>

    <el-dialog title="修订会议纪要" v-model="data.editVisible" width="62%" top="4vh" destroy-on-close>
      <el-form label-position="top" class="edit-form">
        <!-- 会议摘要是纯文本，直接绑定 data.form.summary -->
        <el-form-item label="会议摘要"
          ><el-input v-model="data.form.summary" type="textarea" :rows="5"></el-input
        ></el-form-item>
        <!-- 五块数组内容用同一个组件，v-model 双向绑定到 data.form 上对应的数组 -->
        <edit-section
          title="议题总结"
          v-model="data.form.topics"
          :fields="[
            ['title', '议题'],
            ['summary', '总结']
          ]"
        />
        <edit-section
          title="发言观点"
          v-model="data.form.viewpoints"
          :fields="[
            ['speaker', '发言人'],
            ['viewpoint', '观点']
          ]"
        />
        <edit-section
          title="会议决策"
          v-model="data.form.decisions"
          :fields="[
            ['content', '决策'],
            ['basis', '依据'],
            ['owner_suggestion', '建议负责人'],
            ['deadline_suggestion', '建议期限']
          ]"
        />
        <edit-section
          title="待确认事项"
          v-model="data.form.pending_items"
          :fields="[
            ['content', '事项'],
            ['owner_suggestion', '建议负责人'],
            ['deadline_suggestion', '建议期限']
          ]"
        />
        <edit-section
          title="风险与争议点"
          v-model="data.form.risks"
          :fields="[
            ['content', '内容'],
            ['level', '级别'],
            ['suggestion', '建议']
          ]"
        />
      </el-form>
      <template #footer
        ><el-button @click="data.editVisible = false">取消</el-button
        ><el-button type="primary" @click="save">保存修订</el-button></template
      >
    </el-dialog>

    <!-- 纪要问答弹窗：只上传问题和最近 3 轮历史，回答里的引用可点开看原文 -->
    <el-dialog title="纪要问答" v-model="data.qaVisible" width="min(92vw, 640px)" top="5vh" @closed="resetQa">
      <div ref="qaBodyRef" class="qa-body">
        <el-empty
          v-if="!data.qaMessages.length"
          description="可以问：谁负责什么？有哪些风险？最终结论是什么？"
          :image-size="60"
        />
        <div
          v-for="(msg, idx) in data.qaMessages"
          :key="idx"
          class="qa-message"
          :class="msg.role === 'user' ? 'qa-user' : 'qa-assistant'"
        >
          <div class="qa-role">{{ msg.role === 'user' ? '我' : '助手' }}</div>
          <div class="qa-content">{{ msg.content }}</div>
          <!-- 引用标签：点开可以看到时间、说话人和原文 -->
          <div v-if="msg.citations && msg.citations.length" class="qa-citations">
            <el-tag v-for="(c, ci) in msg.citations" :key="ci" class="qa-citation" @click="showCitation(c)">
              {{ citationLabel(c) }}
            </el-tag>
          </div>
        </div>
      </div>
      <div class="qa-input">
        <el-input
          v-model="data.qaInput"
          type="textarea"
          :rows="2"
          maxlength="500"
          placeholder="请输入问题（Enter 发送，Shift+Enter 换行）"
          @keydown.enter.exact.prevent="sendQa"
        />
        <div class="qa-actions">
          <el-button :disabled="!data.qaMessages.length" @click="clearQa">清空</el-button>
          <el-button type="primary" :loading="data.qaLoading" @click="sendQa">发送</el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { defineComponent, h, nextTick, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import router from '@/router/index.js'
import { ElButton, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import request from '@/utils/request.js'

// 风险级别是后端用 Literal["LOW", "MEDIUM", "HIGH"] 校验的枚举值，
// 页面上一律用下拉框选择、用中文展示，手输中文会让保存请求被后端拒绝
const levelOptions = [
  { label: '低', value: 'LOW' },
  { label: '中', value: 'MEDIUM' },
  { label: '高', value: 'HIGH' }
]
// 把存储值翻译成中文，未知值原样返回
const levelText = level => levelOptions.find(item => item.value === level)?.label || level || '-'
// 详情里每个字段的展示值：风险级别翻成中文，其余为空时显示短横线
const fieldText = (key, value) => (key === 'level' ? levelText(value) : value || '-')
// 旧数据里可能存着中文级别或空值，打开修订时统一转成后端枚举值，避免保存时参数校验失败
const normalizeLevel = level => {
  if (levelOptions.some(item => item.value === level)) {
    return level
  }
  return { '低': 'LOW', '中': 'MEDIUM', '高': 'HIGH' }[level] || 'MEDIUM'
}

// 把毫秒时间戳格式化成 mm:ss，用于证据/引用的时间标签
const formatMs = ms => {
  const value = Number(ms)
  if (!Number.isFinite(value) || value < 0) {
    return '00:00'
  }
  const total = Math.floor(value / 1000)
  const mm = String(Math.floor(total / 60)).padStart(2, '0')
  const ss = String(total % 60).padStart(2, '0')
  return `${mm}:${ss}`
}
// 点“原文”：把这条纪要的 evidence 数组渲染成「[mm:ss] 说话人：原文」的只读弹窗。
// 用 h() 生成 VNode，纯文本节点，不会把转写原文里的内容当 HTML 执行。
const showEvidence = item => {
  const list = Array.isArray(item?.evidence) ? item.evidence : []
  if (!list.length) {
    return
  }
  ElMessageBox.alert(
    h('div', null, list.map((citation, index) =>
      h('div', { key: index, style: 'line-height: 1.8; margin-bottom: 8px' }, [
        h('span', { style: 'color: #2563eb; margin-right: 4px' },
          `[${formatMs(citation.start_ms)}${citation.end_ms ? '-' + formatMs(citation.end_ms) : ''}]`),
        h('span', { style: 'color: #4a5568' }, `${citation.speaker || '未知说话人'}：`),
        h('span', null, citation.quote || '')
      ])
    )),
    '原文出处',
    { confirmButtonText: '知道了' }
  ).catch(() => {})
}

const MinutesSection = defineComponent({
  // title 是这一块的标题，items 是数组内容，fields 决定取哪些字段、显示成什么中文
  props: { title: String, items: Array, fields: Array },
  setup(props) {
    return () =>
      h('section', [
        h('h3', props.title),
        ...(props.items?.length
          ? props.items.map((item, index) =>
              h('div', { class: 'minutes-item', key: index }, [
                // 条目前面标上序号，和修订弹窗里的序号对得上
                h('strong', `${index + 1}. `),
                // 每个字段一行「中文标题：值」，值为空时显示短横线，风险级别显示中文
                ...props.fields.map(field =>
                  h('p', { key: field[0] }, `${field[1]}：${fieldText(field[0], item[field[0]])}`)
                ),
                // 后端为这一条绑定了转写原文时，展示“原文”入口；没有 evidence 则不渲染
                ...(Array.isArray(item.evidence) && item.evidence.length
                  ? [h('div', { class: 'minutes-evidence', style: 'margin-top: 6px' }, [
                      h(ElButton, {
                        link: true,
                        type: 'primary',
                        size: 'small',
                        onClick: () => showEvidence(item)
                      }, () => `原文（${item.evidence.length}）`)
                    ])]
                  : [])
              ])
            )
          : [h('p', '无')])
      ])
  }
})

const EditSection = defineComponent({
  // modelValue 是父组件 v-model 传进来的数组，例如 data.form.decisions；fields 决定每条有哪些输入框
  props: { title: String, modelValue: Array, fields: Array },
  // 声明 update:modelValue 事件，父组件的 v-model 靠它接收新数组
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    // “新增”：在原数组末尾追加一个空白条目，fields 里每个字段都初始化成空字符串
    // 风险级别给了合法的默认值，避免新增的条目没选级别就保存时被后端枚举校验拒绝
    const add = () =>
      emit('update:modelValue', [
        ...(props.modelValue || []),
        Object.fromEntries(props.fields.map(field => [field[0], field[0] === 'level' ? 'MEDIUM' : '']))
      ])
    // 删除图标：过滤掉下标为 index 的条目，把剩下的数组交还给父组件
    const remove = index =>
      emit(
        'update:modelValue',
        props.modelValue.filter((_, itemIndex) => itemIndex !== index)
      )
    return () =>
      h('div', { class: 'edit-section' }, [
        // 每块顶部：左边是标题，右边是“新增”按钮
        h('div', { class: 'edit-title' }, [
          h('strong', props.title),
          h(ElButton, { type: 'primary', link: true, onClick: add }, () => '新增')
        ]),
        // 数组里每个条目渲染成一块 edit-row
        ...(props.modelValue || []).map((item, index) =>
          h('div', { class: 'edit-row', key: index }, [
            // 条目顶部一行：左边序号，右边删除图标，删除按钮不再跟着字段一起换行
            h('div', { class: 'edit-row-head' }, [
              h('span', { class: 'edit-index' }, `${index + 1}`),
              h(ElButton, {
                class: 'edit-remove',
                type: 'danger',
                link: true,
                icon: Delete,
                title: '删除这一条',
                onClick: () => remove(index)
              })
            ]),
            h(
              'div',
              { class: 'edit-fields' },
              props.fields.map(field => {
                // 摘要、正文这类长文本单独占一整行，短字段才并排
                const isLongText = ['summary', 'content', 'viewpoint'].includes(field[0])
                // 每个输入框上面挂一行字段名。只靠 placeholder 的话，一旦填了内容
                // 就看不出这个框是负责人还是期限了
                return h('div', { key: field[0], class: isLongText ? 'edit-field edit-field-full' : 'edit-field' }, [
                  h('label', { class: 'edit-field-label' }, field[1]),
                  // 风险级别只能从 LOW/MEDIUM/HIGH 里选，其余字段用普通输入框
                  field[0] === 'level'
                    ? h(
                        ElSelect,
                        {
                          // 下拉框直接读写条目对象上的 level 字段
                          modelValue: item[field[0]],
                          'onUpdate:modelValue': value => {
                            item[field[0]] = value
                          },
                          placeholder: '请选择' + field[1],
                          style: 'width: 100%'
                        },
                        () => levelOptions.map(option => h(ElOption, { key: option.value, label: option.label, value: option.value }))
                      )
                    : h(ElInput, {
                        // 输入框的值直接读写条目对象上的字段，item 是 data.form 数组里的同一个对象
                        modelValue: item[field[0]],
                        'onUpdate:modelValue': value => {
                          item[field[0]] = value
                        },
                        placeholder: field[1],
                        type: isLongText ? 'textarea' : 'text',
                        autosize: { minRows: 2, maxRows: 5 }
                      })
                ])
              })
            )
          ])
        )
      ])
  }
})

const route = useRoute()
const data = reactive({
  // 顶部会议下拉框的值；从转写任务页“生成纪要”跳过来时，初值取路由 query.meetingId
  meetingId: Number(route.query.meetingId) || null,
  // 纪要状态下拉框，作为 selectPage 的 status 参数
  status: '',
  // 会议下拉框的选项，由 loadMeetings 请求 /meeting/selectOptions 填充
  meetings: [],
  // 当前页码，查询和重置时回到 1
  pageNum: 1,
  // 每页条数，随 selectPage 请求提交
  pageSize: 10,
  // selectPage 返回的总条数，为 0 时分页条不渲染
  total: 0,
  // selectPage 返回的当前页纪要，表格逐行渲染，定时器也按它判断是否还有生成中的纪要
  tableData: [],
  // 控制详情弹窗显示，第 12 章的 showDetail 置为 true
  detailVisible: false,
  // 控制修订弹窗显示，第 12 章的 openEdit 置为 true
  editVisible: false,
  // 详情弹窗展示的纪要，第 12 章的 showDetail 写入
  detail: {},
  // 修订弹窗的表单，第 12 章的 openEdit 写入
  form: {},
  // 详情底部行动项：showDetail 打开时按纪要ID加载
  actionItems: [],
  // 详情里“生成待办”按钮的 loading
  generatingActions: false,
  // “确认生效”中的行动项ID，只让被点的那一条转圈
  confirmingActionId: null,
  // 问答弹窗状态：消息列表只保留当前会话，关闭时清空
  qaVisible: false,
  qaMinutesId: null,
  qaMessages: [],
  qaInput: '',
  qaLoading: false
})
// 问答消息区容器，发送后滚到最新一条
const qaBodyRef = ref()
const statusOptions = [
  { label: '生成中', value: 'GENERATING' },
  { label: '待确认', value: 'DRAFT' },
  { label: '已确认', value: 'CONFIRMED' },
  { label: '生成失败', value: 'FAILED' }
]
// 状态值翻译成中文，状态下拉框的选项和表格状态标签共用 statusOptions
const statusText = status => statusOptions.find(item => item.value === status)?.label || status
// 每种状态对应的标签颜色：生成中橙色、待确认灰色、已确认绿色、失败红色
const statusTag = status =>
  ({ GENERATING: 'warning', DRAFT: 'info', CONFIRMED: 'success', FAILED: 'danger' })[status] || ''

// ===== 详情底部行动项的展示辅助（与行动项页保持同一套中文/颜色口径） =====
const actionStatusText = status =>
  ({ DRAFT: '草稿', OPEN: '待办', DONE: '已完成', CANCELLED: '已撤回' })[status] || status || '-'
const actionStatusTag = status => ({ DRAFT: 'info', OPEN: 'warning', DONE: 'success', CANCELLED: 'info' })[status] || ''
const sourceOfAction = item => item?.source || item?.source_field || ''
const actionOwner = item =>
  item?.owner_name || item?.owner_text || (item?.owner_user_id ? `#${item.owner_user_id}` : '未指定')
const actionDue = item => item?.due_date || item?.due_text || '-'

// ===== 问答弹窗辅助 =====
// 引用标签文字：[mm:ss] 说话人
const citationLabel = citation => `[${formatMs(citation.start_ms)}] ${citation.speaker || '说话人'}`
// 点引用标签：弹窗展示时间范围、说话人和完整原文
const showCitation = citation => {
  ElMessageBox.alert(
    h('div', { style: 'line-height: 1.9' }, [
      h('p', { style: 'margin: 0 0 6px' },
        `时间：${formatMs(citation.start_ms)}${citation.end_ms ? ' - ' + formatMs(citation.end_ms) : ''}`),
      h('p', { style: 'margin: 0 0 6px' }, `说话人：${citation.speaker || '未知说话人'}`),
      h('p', { style: 'margin: 0' }, `原文：${citation.quote || ''}`)
    ]),
    '引用原文',
    { confirmButtonText: '知道了' }
  ).catch(() => {})
}

// 打开详情时顺带加载这份纪要下的行动项；接口失败时保持空列表，页面提供“前往行动项”入口
const loadActionItems = minutesId => {
  data.actionItems = []
  if (!minutesId) {
    return
  }
  request.get('/actionItem/selectByMinutes/' + minutesId).then(res => {
    if (res.code === '200') {
      const payload = res.data
      data.actionItems = Array.isArray(payload) ? payload : (payload?.list || [])
    } else {
      ElMessage.error(res.msg || '加载行动项失败')
    }
  }).catch(() => {})
}

// 详情底部“生成待办”：把已确认纪要的待确认事项转成行动项草稿
const generateActions = () => {
  if (!data.detail.id) {
    return
  }
  data.generatingActions = true
  request.post('/actionItem/generate/' + data.detail.id).then(res => {
    if (res.code === '200') {
      const created = res.data?.created ?? res.data?.created_count ?? 0
      const skipped = res.data?.skipped ?? res.data?.skipped_count ?? 0
      ElMessage.success(`生成完成：新增 ${created} 条${skipped ? `，跳过 ${skipped} 条` : ''}`)
      loadActionItems(data.detail.id)
    } else {
      ElMessage.error(res.msg || '生成待办失败')
    }
  }).catch(() => {}).finally(() => {
    data.generatingActions = false
  })
}

// 表格行上的“生成待办”，和详情里的按钮走同一个接口
const generateFromRow = row => {
  request.post('/actionItem/generate/' + row.id).then(res => {
    if (res.code === '200') {
      const created = res.data?.created ?? res.data?.created_count ?? 0
      const skipped = res.data?.skipped ?? res.data?.skipped_count ?? 0
      ElMessage.success(`生成完成：新增 ${created} 条${skipped ? `，跳过 ${skipped} 条` : ''}`)
    } else {
      ElMessage.error(res.msg || '生成待办失败')
    }
  }).catch(() => {})
}

// 草稿行动项“确认生效”：复用批量确认接口，只传一个 ID
const confirmActionItem = item => {
  data.confirmingActionId = item.id
  request.put('/actionItem/confirmBatch', [item.id]).then(res => {
    if (res.code === '200') {
      ElMessage.success('已确认生效')
      loadActionItems(data.detail.id)
    } else {
      ElMessage.error(res.msg || '确认失败')
    }
  }).catch(() => {}).finally(() => {
    data.confirmingActionId = null
  })
}

// 待办行动项“完成”
const completeActionItem = item => {
  request.put('/actionItem/status/' + item.id, { status: 'DONE' }).then(res => {
    if (res.code === '200') {
      ElMessage.success('待办已完成')
      loadActionItems(data.detail.id)
    } else {
      ElMessage.error(res.msg || '操作失败')
    }
  }).catch(() => {})
}

// ===== 问答弹窗 =====
const resetQa = () => {
  data.qaMessages = []
  data.qaInput = ''
  data.qaLoading = false
  data.qaMinutesId = null
}
const openQa = () => {
  if (!data.detail.id) {
    return
  }
  data.qaMinutesId = data.detail.id
  data.qaMessages = []
  data.qaInput = ''
  data.qaVisible = true
}
const scrollQaToBottom = () => {
  nextTick(() => {
    if (qaBodyRef.value) {
      qaBodyRef.value.scrollTop = qaBodyRef.value.scrollHeight
    }
  })
}
const clearQa = () => {
  data.qaMessages = []
  data.qaInput = ''
}
const sendQa = () => {
  const question = (data.qaInput || '').trim()
  if (!question || data.qaLoading || !data.qaMinutesId) {
    return
  }
  // 只带最近 3 轮历史（一问一答算 2 条，最多 6 条），且只保留 role/content 两个字段
  const history = data.qaMessages.slice(-6).map(message => ({ role: message.role, content: message.content }))
  data.qaMessages.push({ role: 'user', content: question })
  data.qaInput = ''
  data.qaLoading = true
  scrollQaToBottom()
  request.post('/meetingMinutes/ask/' + data.qaMinutesId, { question, history }).then(res => {
    if (res.code === '200') {
      const answer = res.data || {}
      data.qaMessages.push({
        role: 'assistant',
        content: answer.answer || (answer.found === false ? '纪要中没有提到相关信息' : '暂无回答'),
        citations: Array.isArray(answer.citations) ? answer.citations : []
      })
    } else {
      data.qaMessages.push({ role: 'assistant', content: res.msg || '问答失败，请稍后重试' })
    }
  }).catch(() => {
    data.qaMessages.push({ role: 'assistant', content: '问答请求失败，请稍后重试' })
  }).finally(() => {
    data.qaLoading = false
    scrollQaToBottom()
  })
}

const load = () =>
  request
    .get('/meetingMinutes/selectPage', {
      params: {
        // 会议下拉框的值；为 null 时转成 undefined，Axios 不拼这个参数，后端查全部有权限的会议
        meetingId: data.meetingId || undefined,
        // 纪要状态下拉框的值
        status: data.status,
        pageNum: data.pageNum,
        pageSize: data.pageSize
      }
    })
    .then(res => {
      if (res.code === '200') {
        // 当前页纪要写入表格数据源
        data.tableData = res.data?.list || []
        // 总条数交给分页条
        data.total = res.data?.total || 0
      } else ElMessage.error(res.msg)
    })
const loadMeetings = () =>
  // GET /meeting/selectOptions 返回当前用户可访问的会议，接口本身在第 6 章第 6 节讲过
  request.get('/meeting/selectOptions').then(res => {
    if (res.code === '200') {
      // 填充会议下拉框的选项
      data.meetings = res.data || []
      // 会议选项就位后再拉纪要列表，初始的 meetingId 来自路由参数
      load()
    } else ElMessage.error(res.msg)
  })
const showDetail = id =>
  request.get('/meetingMinutes/selectById/' + id).then(res => {
    if (res.code === '200') {
      // 详情接口返回的字段和列表接口完全一样，六块内容（含每条 evidence）都在里面
      data.detail = res.data
      data.detailVisible = true
      // 底部行动项区块单独拉一次：接口失败时保持空列表，不影响纪要正文展示
      loadActionItems(res.data?.id)
    } else ElMessage.error(res.msg)
  }).catch(() => {})
const openEdit = id =>
  request.get('/meetingMinutes/selectById/' + id).then(res => {
    if (res.code === '200') {
      // 深拷贝一份进 data.form，弹窗里改到一半点取消不会污染表格数据；
      // evidence 数组随深拷贝原样保留，修订只改业务字段，保存时 payload 带回原 evidence
      data.form = JSON.parse(JSON.stringify(res.data))
      // 风险级别归一化成 LOW/MEDIUM/HIGH，修订弹窗的下拉框才能正确回显
      data.form.risks = (data.form.risks || []).map(item => ({ ...item, level: normalizeLevel(item.level) }))
      data.editVisible = true
    } else ElMessage.error(res.msg)
  }).catch(() => {})
const save = () => {
  // 会议摘要为空或只有空白时直接提示，不发请求
  if (!data.form.summary?.trim()) return ElMessage.warning('会议摘要不能为空')
  // 整个 data.form 提交上去，后端只取 id 和六块内容；
  // 六块里每条的 evidence 是 openEdit 深拷贝时保留下来的原值，只编辑业务字段不会丢证据
  request.put('/meetingMinutes/update', data.form).then(res => {
    if (res.code === '200') {
      ElMessage.success('纪要修订已保存')
      // 关闭修订弹窗
      data.editVisible = false
      // 重新拉列表，更新时间跟着刷新
      load()
    } else ElMessage.error(res.msg)
  }).catch(() => {})
}
const confirmMinutes = row =>
  // 确认之后修订入口就消失了，所以先弹一次确认框说明这一点
  ElMessageBox.confirm(`确认将${row.meeting_title}的会议纪要设为已确认吗？确认后不能继续修订。`, '确认纪要', {
    type: 'warning'
  }).then(() =>
    request.put('/meetingMinutes/confirm/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('会议纪要已确认')
        // 重新拉列表，状态变成“已确认”，修订和确认按钮消失，取消确认按钮出现
        load()
      } else ElMessage.error(res.msg)
    })
  )
// 取消确认把纪要退回草稿，内容才能继续修订或者被自检的修订稿覆盖
const unconfirmMinutes = row =>
  ElMessageBox.confirm(
    `确定把${row.meeting_title}的会议纪要退回草稿吗？退回后可以继续修订，也可以让 AI 自检的修订稿覆盖它。`,
    '取消确认',
    { type: 'warning', confirmButtonText: '确定退回', cancelButtonText: '取消' }
  ).then(() =>
    // 点“确定退回”后才发 PUT /meetingMinutes/unconfirm/{minutes_id}，纪要ID取自表格行
    request.put('/meetingMinutes/unconfirm/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('纪要已退回草稿')
        // 重新拉列表，这一行状态变回“待确认”，修订和确认按钮重新出现
        load()
      // 例如“只有管理员或纪要创建人可以取消确认”
      } else ElMessage.error(res.msg)
    })
  )

const del = row => {
  // 确认框提示删除后要回到转写任务页重新生成
  ElMessageBox.confirm(
      `确定删除“${row.meeting_title}”的会议纪要吗？删除后需要重新从转写任务生成。`,
      '删除会议纪要',
      { type: 'warning', confirmButtonText: '确定删除', cancelButtonText: '取消' }
  ).then(() => {
    // 点“确定删除”后才发 DELETE /meetingMinutes/delete/{minutes_id}
    request.delete('/meetingMinutes/delete/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 重新拉列表，被删的纪要从表格消失
        load()
      } else {
        // 例如“只有管理员或纪要创建人可以删除”
        ElMessage.error(res.msg)
      }
    })
  })
}

const startReview = row =>
  ElMessageBox.confirm(
    'Agent 将对照转写原文审查这份纪要，发现问题后生成修订稿，修订稿由人工确认后应用。确定开始吗？',
    '发起纪要自检',
    { type: 'warning' }
  ).then(() =>
    // 纪要主键来自所点击的表格行，每次运行最多执行两轮审查与重写。
    request.post('/agent/start', { minutes_id: row.id, max_rounds: 2 }).then(res => {
      if (res.code === '200') {
        ElMessage.success('自检已发起，请在运行列表查看详情')
        // 进入运行台后由列表查询和详情事件流展示本次自检。
        router.push('/manager/agent')
      } else ElMessage.error(res.msg)
    })
  )

const retry = row =>
  // 失败纪要行的重试按钮先显示确认框，取消时不提交。
  ElMessageBox.confirm('确定重新生成这份纪要吗？', '重试确认', { type: 'warning' }).then(() =>
    // 当前行纪要 ID 拼入 PUT 地址，后端重新调度该纪要的生成。
    request.put('/meetingMinutes/retry/' + row.id).then(res => {
      if (res.code === '200') {
        ElMessage.success('纪要已重新提交生成')
        // 业务成功后刷新列表，显示 GENERATING 状态和生成进度。
        load()
      } else ElMessage.error(res.msg)
    })
  )
const download = (row, type) =>
  // responseType 设成 blob，否则二进制文件会被当成文本处理坏掉
  request.get(`/meetingMinutes/export/${row.id}/${type}`, { responseType: 'blob' }).then(blob => {
    // 后端业务异常返回的 JSON 已经由 request.js 的 blob 分支识别并拒绝，
    // 能走到这里的 blob 一定就是文件本身，不再重复判断 JSON
    // 给 blob 造一个临时地址，用一个隐藏的 a 标签触发浏览器下载
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    // 文件名用页面上已有的会议主题拼，后缀按导出类型区分
    link.download = `${row.meeting_title}-会议纪要.${type === 'word' ? 'docx' : 'pdf'}`
    link.click()
    // 等浏览器开始下载后再释放临时地址，避免个别浏览器在点击同步返回前就取消下载
    setTimeout(() => URL.revokeObjectURL(url), 0)
  })
const search = () => {
  data.pageNum = 1
  load()
}
const reset = () => {
  // 清空会议和状态两个条件，列表回到当前用户可访问的全部纪要
  data.meetingId = null
  data.status = ''
  data.pageNum = 1
  load()
}
// 每三秒检查一次，列表里还有生成中的纪要才重新拉，全部跑完就不再发请求
const timer = window.setInterval(() => {
  if (data.tableData.some(item => item.status === 'GENERATING')) load()
}, 3000)
// 离开页面时清掉定时器，否则路由切走了还在后台发请求
onUnmounted(() => window.clearInterval(timer))
// 页面打开时先拉会议下拉，loadMeetings 成功后再调用 load
loadMeetings()
</script>

<style scoped>
.minutes-content h3 {
  margin: 20px 0 8px;
}
.minutes-content p {
  line-height: 1.7;
  white-space: pre-wrap;
}
.minutes-item {
  padding: 10px 14px;
  margin-bottom: 8px;
  background: #f7f8fa;
  border-radius: 4px;
}
.minutes-item p {
  margin: 5px 0;
}
/* 表单区留出左右内边距，内容过长时在弹窗内部滚动，底部按钮始终看得见 */
.edit-form {
  padding: 0 16px;
  max-height: 72vh;
  overflow-y: auto;
}

/* 下面这些元素是 EditSection 用渲染函数生成的，不带 scoped 标记，
   必须用 :deep 穿透进去，否则样式匹配不到，条目会退回浏览器默认的竖向堆叠 */
:deep(.edit-section) {
  margin-bottom: 20px;
}

:deep(.edit-title) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 8px;
  margin-bottom: 10px;
  border-bottom: 1px solid #ebeef3;
  color: #1f2d3d;
}

:deep(.edit-row) {
  padding: 10px 12px 12px;
  margin-bottom: 10px;
  background: #f8fafc;
  border: 1px solid #ebeef3;
  border-radius: 6px;
}

:deep(.edit-row-head) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

:deep(.edit-index) {
  font-size: 13px;
  color: #8a94a6;
}

:deep(.edit-remove) {
  padding: 0;
  height: auto;
}

/* 短字段并排，长文本靠 edit-field-full 独占一行 */
:deep(.edit-fields) {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px 12px;
}

:deep(.edit-field-full) {
  grid-column: 1 / -1;
}

/* 输入框上面那行字段名，填了内容之后也能看出这个框是什么 */
:deep(.edit-field-label) {
  display: block;
  margin-bottom: 4px;
  font-size: 12px;
  color: #8a94a6;
}

/* 详情顶部工具栏：问答入口 */
.minutes-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.minutes-toolbar-tip {
  color: #8a94a6;
  font-size: 12px;
}

/* 详情底部行动项区块 */
.minutes-actions {
  margin-top: 18px;
  padding: 14px;
  background: #fafbfc;
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.minutes-actions-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.minutes-actions-head h3 {
  margin: 0;
  margin-top: 0 !important;
}

.minutes-actions-tools {
  display: flex;
  align-items: center;
}

.minutes-actions-link {
  color: var(--mb-brand);
  cursor: pointer;
  font-size: 13px;
  margin-left: 12px;
}

.minutes-action {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 0;
  border-top: 1px solid #eef0f4;
}

.minutes-action:first-child {
  border-top: none;
}

.minutes-action-main {
  min-width: 0;
}

.minutes-action-content {
  color: #1f2d3d;
  line-height: 1.7;
  word-break: break-word;
}

.minutes-action-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 5px;
  font-size: 12px;
  color: #8a94a6;
}

.minutes-action-ai {
  color: #10b981;
}

.minutes-action-ops {
  flex-shrink: 0;
}

/* 问答弹窗 */
.qa-body {
  max-height: 50vh;
  overflow-y: auto;
  padding-right: 4px;
}

.qa-message {
  margin-bottom: 12px;
}

.qa-role {
  font-size: 12px;
  color: #8a94a6;
  margin-bottom: 4px;
}

.qa-content {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
  color: #1f2d3d;
}

.qa-user .qa-content {
  background: #eff6ff;
  border-radius: 6px;
  padding: 8px 10px;
}

.qa-assistant .qa-content {
  background: #f7f8fa;
  border-radius: 6px;
  padding: 8px 10px;
}

.qa-citations {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.qa-citation {
  cursor: pointer;
}

.qa-input {
  margin-top: 12px;
}

.qa-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

/* 窄屏：工具栏换行、行动项上下堆叠 */
@media (max-width: 900px) {
  .minutes-toolbar {
    flex-wrap: wrap;
  }

  .minutes-actions-head {
    flex-wrap: wrap;
    gap: 6px;
  }

  .minutes-action {
    flex-direction: column;
  }

  .qa-body {
    max-height: 44vh;
  }
}

</style>
