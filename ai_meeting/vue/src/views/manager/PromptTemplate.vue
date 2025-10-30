<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 模板名称输入框，写入 data.name，后端按名称模糊匹配 -->
      <el-input v-model="data.name" style="width: 240px; margin-right: 10px" placeholder="请输入模板名称"></el-input>
      <!-- 应用场景下拉框，写入 data.sceneType，三个取值与数据库的 scene_type 一致；clearable 清空后查全部 -->
      <el-select
        v-model="data.sceneType"
        clearable
        style="width: 180px; margin-right: 10px"
        placeholder="请选择应用场景"
        ><el-option label="会议纪要" value="MEETING_MINUTES" /><el-option label="纪要自检" value="AGENT" /><el-option
          label="音视频转写"
          value="TRANSCRIPTION"
      /></el-select>
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
    </div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 提示条说明这五条模板服务哪些功能，以及变量用花括号表示、保存时会校验必需变量 -->
      <el-alert
        title="系统预置纪要生成、纪要自检和说话人匹配模板。变量使用花括号表示，保存时会校验必需变量。"
        type="info"
        :closable="false"
      />
    </div>
    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData">
        <el-table-column prop="name" label="模板名称" min-width="220" />
        <!-- 场景列展示中文，原始值是 scene_type 里的英文标识 -->
        <el-table-column prop="scene_type" label="应用场景" width="160">
          <template #default="scope">{{ sceneText(scope.row.scene_type) }}</template>
        </el-table-column>
        <!-- 状态列：enabled 为 true 显示绿色“已启用”，否则灰色“未启用”；业务代码只取已启用的模板 -->
        <el-table-column prop="enabled" label="状态" width="90">
          <template #default="scope"
            ><el-tag :type="scope.row.enabled ? 'success' : 'info'">{{
              scope.row.enabled ? '已启用' : '未启用'
            }}</el-tag></template
          >
        </el-table-column>
        <el-table-column prop="update_time" label="更新时间" width="175" />
        <!-- 只有编辑一个入口，没有新增和删除 -->
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="scope"
            ><el-button link type="primary" @click="handleEdit(scope.row)">编辑</el-button></template
          >
        </el-table-column>
      </el-table>
    </div>
    <div class="card" v-if="data.total">
      <!-- current-change 触发 load，pageNum 已被双向绑定改成新页码 -->
      <el-pagination
        v-model:current-page="data.pageNum"
        background
        layout="prev, pager, next"
        :page-size="data.pageSize"
        :total="data.total"
        @current-change="load"
      />
    </div>

    <el-dialog title="编辑Prompt模板" v-model="data.formVisible" width="70%" top="4vh" destroy-on-close>
      <el-form ref="formRef" :model="data.form" :rules="data.rules" label-width="100px">
        <!-- 模板名称可改，写入 data.form.name -->
        <el-form-item label="模板名称" prop="name"><el-input v-model="data.form.name"></el-input></el-form-item>
        <!-- 应用场景只读展示，没有输入框；保存时 data.form 里的原值随请求一起提交 -->
        <el-form-item label="应用场景"><span style="color: #4a5568">{{ sceneText(data.form.scene_type) }}</span></el-form-item>
        <!-- 系统指令文本域，写入 data.form.system_prompt，调模型时作为系统消息发送 -->
        <el-form-item label="系统指令" prop="system_prompt"
          ><el-input v-model="data.form.system_prompt" type="textarea" :rows="7"></el-input
        ></el-form-item>
        <!-- 用户模板文本域，写入 data.form.user_prompt，里面的花括号变量保存时会被后端校验 -->
        <el-form-item label="用户模板" prop="user_prompt"
          ><el-input v-model="data.form.user_prompt" type="textarea" :rows="10"></el-input
        ></el-form-item>
        <!-- 启用开关，关掉之后对应功能取不到模板会直接报错 -->
        <el-form-item label="启用模板"><el-switch v-model="data.form.enabled"></el-switch></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="data.formVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import request from '@/utils/request.js'

// 应用场景在页面上只读，取值由系统预置，这里把存库的英文标识翻成中文展示
const sceneText = value =>
  ({
    MEETING_MINUTES: '会议纪要',
    AGENT: '纪要自检',
    TRANSCRIPTION: '音视频转写'
  })[value] || value

// 指向编辑弹窗里的 el-form，save 通过它调用 validate
const formRef = ref()
const data = reactive({
  // 模板名称查询框绑定它，作为 name 参数发给后端做模糊匹配
  name: '',
  // 应用场景下拉框绑定它，作为 sceneType 参数发给后端做精确匹配
  sceneType: '',
  // 当前页码，由分页控件双向绑定
  pageNum: 1,
  // 每页条数，随查询一起提交
  pageSize: 10,
  // 后端返回的总条数，为 0 时分页条不渲染
  total: 0,
  // 表格数据，来自 /promptTemplate/selectPage 的 list
  tableData: [],
  // 控制编辑弹窗显示，handleEdit 里置为 true
  formVisible: false,
  // 编辑弹窗的表单对象，handleEdit 深拷贝当前行填充
  form: {},
  // 三条必填校验，和后端 validate_prompt 的第一条判断对应
  rules: {
    name: [{ required: true, message: '请输入模板名称', trigger: 'blur' }],
    system_prompt: [{ required: true, message: '请输入系统指令', trigger: 'blur' }],
    user_prompt: [{ required: true, message: '请输入用户模板', trigger: 'blur' }]
  }
})

const load = () => {
  request
    .get('/promptTemplate/selectPage', {
      // 四个查询参数：名称模糊词、场景、页码和每页条数
      params: { name: data.name, sceneType: data.sceneType, pageNum: data.pageNum, pageSize: data.pageSize }
    })
    .then(res => {
      if (res.code === '200') {
        // list 填进表格，total 交给分页控件算页数
        data.tableData = res.data?.list || []
        data.total = res.data?.total || 0
      } else ElMessage.error(res.msg)
    })
}

const handleEdit = row => {
  // 深拷贝当前行，弹窗里改到一半点取消时不会影响表格上正在显示的那一行
  // 拷贝进来的对象带着 id 和 code，保存时原样提交给后端
  data.form = JSON.parse(JSON.stringify(row))
  data.formVisible = true
}

const save = () => {
  formRef.value.validate(valid => {
    // 三条必填校验没通过时停在弹窗上，不发请求
    if (!valid) return
    // data.form 整体作为 JSON 提交，带着 id 和 code，后端按 code 校验必需变量
    request.put('/promptTemplate/update', data.form).then(res => {
      if (res.code === '200') {
        ElMessage.success('Prompt模板保存成功')
        // 关闭编辑弹窗
        data.formVisible = false
        // 按当前查询条件重拉列表，“更新时间”列刷新
        load()
      // 例如“用户模板缺少必需变量：{transcript}”，弹窗保持打开
      } else ElMessage.error(res.msg)
    })
  })
}

const search = () => {
  // 换了查询条件回到第一页，避免停在超出范围的页码上
  data.pageNum = 1
  load()
}
const reset = () => {
  // 清空两个查询条件并回到第一页，再拉一次全量
  data.name = ''
  data.sceneType = ''
  data.pageNum = 1
  load()
}
load()
</script>
