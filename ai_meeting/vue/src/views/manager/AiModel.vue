<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 配置名称输入框，写入 data.name，后端按名称模糊匹配 -->
      <el-input v-model="data.name" style="width: 240px; margin-right: 10px" placeholder="请输入配置名称"></el-input>
      <!-- 模型用途下拉框，写入 data.modelType，四个取值与数据库的 model_type 一致 -->
      <el-select
        v-model="data.modelType"
        clearable
        style="width: 180px; margin-right: 10px"
        placeholder="请选择模型用途"
      >
        <el-option label="音视频转写" value="TRANSCRIPTION"></el-option>
        <el-option label="会议纪要" value="MINUTES"></el-option>
        <el-option label="纪要自检" value="AGENT"></el-option>
        <el-option label="说话人匹配" value="SPEAKER"></el-option>
      </el-select>
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-button type="primary" plain @click="handleAdd">新增AI模型</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="name" label="配置名称" min-width="160" />
        <el-table-column prop="model_type" label="模型用途" width="120">
          <template #default="scope">{{ modelTypeText(scope.row.model_type) }}</template>
        </el-table-column>
        <el-table-column prop="provider" label="服务协议" width="180" />
        <el-table-column prop="base_url" label="API地址" min-width="220" show-overflow-tooltip />
        <el-table-column prop="model_name" label="模型名称" min-width="210" />
        <!-- 这一列显示的是后端做过掩码的字符串 -->
        <el-table-column prop="api_key" label="API Key" min-width="150" />
        <el-table-column prop="timeout_seconds" label="超时秒数" width="100" />
        <el-table-column prop="enabled" label="状态" width="90">
          <template #default="scope">
            <!-- enabled 为 true 显示绿色“已启用”，否则灰色“未启用” -->
            <el-tag :type="scope.row.enabled ? 'success' : 'info'">{{
              scope.row.enabled ? '已启用' : '未启用'
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="handleEdit(scope.row)">编辑</el-button>
            <el-button link type="danger" @click="del(scope.row.id)">删除</el-button>
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

    <el-dialog title="AI模型配置" v-model="data.formVisible" width="48%" destroy-on-close>
      <el-form ref="formRef" :model="data.form" :rules="data.rules" label-width="110px" style="padding: 10px 20px">
        <!-- 配置名称，必填，写入 data.form.name -->
        <el-form-item label="配置名称" prop="name">
          <el-input v-model="data.form.name" placeholder="请输入配置名称"></el-input>
        </el-form-item>
        <!-- 模型用途下拉框，写入 data.form.model_type；change 时联动填默认模型名和超时秒数 -->
        <el-form-item label="模型用途" prop="model_type">
          <el-select v-model="data.form.model_type" style="width: 100%" @change="changeModelType">
            <el-option label="音视频转写" value="TRANSCRIPTION"></el-option>
            <el-option label="会议纪要" value="MINUTES"></el-option>
            <el-option label="纪要自检" value="AGENT"></el-option>
            <el-option label="说话人匹配" value="SPEAKER"></el-option>
          </el-select>
        </el-form-item>
        <!-- 服务协议只读，切换用途时由 changeModelType 自动填，转写和另外两类不是同一种协议 -->
        <el-form-item label="服务协议">
          <el-input v-model="data.form.provider" disabled></el-input>
        </el-form-item>
        <!-- API 地址，必填，写入 data.form.base_url -->
        <el-form-item label="API地址" prop="base_url">
          <el-input v-model="data.form.base_url" :placeholder="baseUrlPlaceholder"></el-input>
        </el-form-item>
        <!-- API Key，必填。编辑时这里是掩码字符串，不动它就表示沿用原 Key -->
        <el-form-item label="API Key" prop="api_key">
          <el-input
            v-model="data.form.api_key"
            type="password"
            show-password
            placeholder="编辑时不修改可保留原Key"
          ></el-input>
        </el-form-item>
        <!-- 模型名称，必填。占位文案随用途变化 -->
        <el-form-item label="模型名称" prop="model_name">
          <el-input v-model="data.form.model_name" :placeholder="modelPlaceholder"></el-input>
        </el-form-item>
        <!-- 超时秒数数字框，范围与后端校验一致 -->
        <el-form-item label="超时秒数" prop="timeout_seconds">
          <el-input-number
            v-model="data.form.timeout_seconds"
            :min="30"
            :max="3600"
            style="width: 100%"
          ></el-input-number>
        </el-form-item>
        <!-- 启用开关，写入 data.form.enabled -->
        <el-form-item label="启用配置">
          <el-switch v-model="data.form.enabled"></el-switch>
          <span style="margin-left: 10px; color: #909399">同一用途只允许启用一个模型</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'

const formRef = ref()

const data = reactive({
  // 顶部配置名称输入框
  name: '',
  // 顶部模型用途下拉框
  modelType: '',
  // 当前页码
  pageNum: 1,
  // 每页条数
  pageSize: 10,
  // 总条数
  total: 0,
  // 表格数据源
  tableData: [],
  // 控制配置弹窗显示
  formVisible: false,
  // 弹窗表单对象，有 id 表示编辑
  form: {},
  // 弹窗必填校验规则
  rules: {
    name: [{ required: true, message: '请输入配置名称', trigger: 'blur' }],
    model_type: [{ required: true, message: '请选择模型用途', trigger: 'change' }],
    base_url: [{ required: true, message: '请输入API地址', trigger: 'blur' }],
    api_key: [{ required: true, message: '请输入API Key', trigger: 'blur' }],
    model_name: [{ required: true, message: '请输入模型名称', trigger: 'blur' }]
  }
})


// 删除最后一条后当前页变空：回退一页再查，避免停在空表
const reloadAfterDelete = () => {
  if ((!data.tableData || data.tableData.length === 1) && data.pageNum > 1) data.pageNum -= 1
  load()
}

const load = () => {
  request
    .get('/aiModel/selectPage', {
      params: {
        // 配置名称输入框的值
        name: data.name,
        // 用途下拉框的值。清空后是空串，转成 undefined 让 Axios 不拼这个参数
        model_type: data.modelType || undefined,
        page_num: data.pageNum,
        page_size: data.pageSize
      }
    })
    .then(res => {
      if (res.code === '200') {
        data.tableData = res.data?.list || []
        data.total = res.data?.total || 0
      } else {
        ElMessage.error(res.msg)
      }
    })
}

const modelTypeText = value =>
  ({
    TRANSCRIPTION: '音视频转写',
    MINUTES: '会议纪要',
    AGENT: '纪要自检',
    SPEAKER: '说话人匹配'
  })[value] || value

// 四类模型的默认服务地址、模型名和超时秒数，和 ai_meeting/ai_meeting.sql 里的初始配置保持一致
// 纪要、纪要自检和说话人匹配走阿里百炼的 OpenAI 兼容地址
// 转写走百炼自己的录音文件识别协议，地址和服务协议都和另外三类不同
const MODEL_DEFAULTS = {
  TRANSCRIPTION: { base_url: 'https://dashscope.aliyuncs.com/api/v1', model_name: 'paraformer-v2', timeout_seconds: 600, provider: 'DASHSCOPE' },
  MINUTES: { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model_name: 'qwen-plus', timeout_seconds: 600, provider: 'OPENAI_COMPATIBLE' },
  AGENT: { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model_name: 'qwen-plus', timeout_seconds: 300, provider: 'OPENAI_COMPATIBLE' },
  SPEAKER: { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model_name: 'qwen-plus', timeout_seconds: 180, provider: 'OPENAI_COMPATIBLE' }
}

// 模型名称输入框的提示文字，随上面选中的模型类型变化
const modelPlaceholder = computed(() => MODEL_DEFAULTS[data.form.model_type]?.model_name || '请输入模型名称')

// 服务地址输入框的提示文字，同样随模型类型变化
const baseUrlPlaceholder = computed(() => MODEL_DEFAULTS[data.form.model_type]?.base_url || '请输入服务地址')

// 模型用途下拉框的 @change 回调，value 是新选中的 model_type
const changeModelType = value => {
  // 切换模型类型时，把该类型的默认地址、模型名和超时一并填进表单，管理员只需要补 API Key
  const preset = MODEL_DEFAULTS[value]
  // 转写和其余三类用的不是同一套协议，服务协议要跟着模型类型走
  data.form.provider = preset?.provider || 'OPENAI_COMPATIBLE'
  // 覆盖“API地址”输入框，原来填的地址不保留
  data.form.base_url = preset?.base_url || ''
  // 覆盖“模型名称”输入框
  data.form.model_name = preset?.model_name || ''
  // 覆盖“超时秒数”数字框：转写 600、纪要 600、纪要自检 300、说话人匹配 180
  data.form.timeout_seconds = preset?.timeout_seconds || 180
}

const handleAdd = () => {
  // 点击新增后创建独立表单，默认选择 TRANSCRIPTION。
  data.form = {
    model_type: 'TRANSCRIPTION',
    // 转写使用 MODEL_DEFAULTS 的 DASHSCOPE 协议值，服务协议输入框只读。
    provider: MODEL_DEFAULTS.TRANSCRIPTION.provider,
    // 新增弹窗默认停在转写类型上，三个字段取 MODEL_DEFAULTS 里对应的预设值
    base_url: MODEL_DEFAULTS.TRANSCRIPTION.base_url,
    model_name: MODEL_DEFAULTS.TRANSCRIPTION.model_name,
    timeout_seconds: MODEL_DEFAULTS.TRANSCRIPTION.timeout_seconds,
    enabled: false
  }
  // 打开配置弹窗，API 地址、模型名、超时和启用状态由上述默认值渲染。
  data.formVisible = true
}

const handleEdit = row => {
  // 深拷贝当前行，弹窗里的改动不影响表格上正在显示的那一行
  // 拷贝进来的 api_key 是掩码字符串，不是真实 Key
  data.form = JSON.parse(JSON.stringify(row))
  data.formVisible = true
}

const save = () => {
  // 先跑五条必填校验
  formRef.value.validate(valid => {
    if (!valid) {
      return
    }
    // 有 id 走编辑，没有 id 走新增
    const action = data.form.id ? request.put('/aiModel/update', data.form) : request.post('/aiModel/add', data.form)
    action.then(res => {
      if (res.code === '200') {
        ElMessage.success('保存成功')
        data.formVisible = false
        // 重新拉列表，启用状态变化后排序也随之变化
        load()
      } else {
        // 例如“启用模型前请填写API Key”“已被业务数据使用的模型不能修改用途”
        ElMessage.error(res.msg)
      }
    })
  })
}

const del = id => {
  ElMessageBox.confirm('确定删除该AI模型配置吗？', '删除确认', { type: 'warning' }).then(() => {
    request.delete('/aiModel/delete/' + id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        reloadAfterDelete()
      } else {
        // 例如“该模型配置已被转写任务使用，不能删除”
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
  // 清空名称和用途两个条件
  data.name = ''
  data.modelType = ''
  data.pageNum = 1
  load()
}

load()
</script>
