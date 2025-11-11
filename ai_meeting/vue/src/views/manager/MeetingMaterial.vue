<template>
  <div>
    <div class="card" style="margin-bottom: 5px">
      <!-- 会议下拉框。filterable 支持输入编号或主题过滤，change 时重新加载资料列表 -->
      <el-select v-model="data.meetingId" filterable style="width: 360px; margin-right: 10px" placeholder="请选择会议" @change="changeMeeting">
        <el-option v-for="item in data.meetings" :key="item.id" :label="meetingLabel(item)" :value="item.id"></el-option>
      </el-select>
      <!-- 文件名输入框，写入 data.fileName，后端按文件名模糊匹配 -->
      <el-input v-model="data.fileName" style="width: 220px; margin-right: 10px" placeholder="请输入文件名"></el-input>
      <!-- 资料类型下拉框，写入 data.fileType，三个取值和数据库里的 file_type 一致 -->
      <el-select v-model="data.fileType" clearable style="width: 140px; margin-right: 10px" placeholder="资料类型">
        <el-option label="录音" value="AUDIO"></el-option>
        <el-option label="视频" value="VIDEO"></el-option>
        <el-option label="附件" value="ATTACHMENT"></el-option>
      </el-select>
      <el-button type="info" plain @click="search">查询</el-button>
      <el-button type="warning" plain style="margin-left: 10px" @click="reset">重置</el-button>
      <el-button type="danger" plain style="margin-left: 10px" @click="delBatch">批量删除</el-button>
    </div>

    <!-- el-upload 是块级元素，外层用 flex 让右侧的格式说明和按钮排在同一行 -->
    <div class="card" style="margin-bottom: 5px; display: flex; align-items: center">
      <el-upload
          :show-file-list="false"
          :http-request="uploadFile"
          :before-upload="beforeUpload"
          :disabled="!data.meetingId || data.uploading > 0"
          multiple
      >
        <!-- 有文件正在上传时按钮进入 loading 状态 -->
        <el-button type="primary" plain :loading="data.uploading > 0">上传会议资料</el-button>
      </el-upload>
      <!-- 上传进度与取消：500MB 级文件不再只有一个转圈的按钮 -->
      <el-progress v-if="data.uploadPercent > 0 && data.uploadPercent < 100"
                   :percentage="data.uploadPercent" style="width: 200px; margin-left: 12px" />
      <el-button v-if="data.uploading > 0" link type="danger" style="margin-left: 8px"
                 @click="cancelUpload">取消上传</el-button>
      <!-- 上传按钮旁显示允许的文件类别、500MB 大小限制和音视频时长提示 -->
      <span style="margin-left: 15px; color: rgba(17, 24, 39, .55)"
        >支持录音、视频、文档、压缩包和图片，单个文件最大500MB；音视频建议上传一小时以内的，太长会影响纪要自检的准确度</span
      >
    </div>

    <div class="card" style="margin-bottom: 5px">
      <el-table stripe :data="data.tableData" @selection-change="handleSelectionChange">
        <!-- 勾选列，批量删除依赖它 -->
        <el-table-column type="selection" width="55" />
        <el-table-column prop="file_name" label="文件名" min-width="260" show-overflow-tooltip />
        <el-table-column prop="file_type" label="资料类型" width="100">
          <template #default="scope">
            <!-- 录音绿色、视频橙色、附件灰色 -->
            <el-tag :type="fileTypeTag(scope.row.file_type)">{{ fileTypeText(scope.row.file_type) }}</el-tag>
          </template>
        </el-table-column>
        <!-- file_size_text 由后端把字节数换算好，前端直接显示 -->
        <el-table-column prop="file_size_text" label="文件大小" width="120" />
        <el-table-column prop="uploader_name" label="上传人" width="120" />
        <el-table-column prop="create_time" label="上传时间" width="175" />
        <el-table-column label="操作" width="270" fixed="right">
          <template #default="scope">
            <!-- 只有录音和视频能发起转写，附件行上不出现这个按钮 -->
            <el-button v-if="['AUDIO', 'VIDEO'].includes(scope.row.file_type)" link type="success" @click="startTranscription(scope.row)">发起转写</el-button>
            <!-- 只有浏览器能直接呈现的格式，以及能读出清单的 zip，才显示查看入口 -->
            <el-button v-if="canPreview(scope.row)" link type="primary" @click="preview(scope.row)">查看</el-button>
            <el-button link type="primary" @click="download(scope.row)">下载</el-button>
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

    <!-- 发起转写弹窗，语言选项对应转写接口的 language 参数 -->
    <el-dialog v-model="data.transcriptionVisible" title="发起转写" width="460px">
      <el-form label-width="90px" style="padding: 6px 10px 0">
        <!-- 只读展示要转写的资料文件名，值由 startTranscription 写进 transcriptionForm.fileName -->
        <el-form-item label="资料">
          <span style="color: rgba(17, 24, 39, .75)">{{ data.transcriptionForm.fileName }}</span>
        </el-form-item>
        <!-- 语言下拉框写入 transcriptionForm.language：四个语言代码，空字符串表示自动识别 -->
        <el-form-item label="音频语言">
          <el-select v-model="data.transcriptionForm.language" style="width: 100%">
            <el-option label="中文" value="zh"></el-option>
            <el-option label="英文" value="en"></el-option>
            <el-option label="日语" value="ja"></el-option>
            <el-option label="韩语" value="ko"></el-option>
            <el-option label="自动识别" value=""></el-option>
          </el-select>
          <div style="color: rgba(17, 24, 39, .55); font-size: 12px; line-height: 1.7; margin-top: 6px">
            选的是这段音频的主要语言。中文会议里夹杂英文词汇时选“中文”即可，模型照常识别其中的英文；
            整段说的是什么语言不确定时选“自动识别”，由模型自己判断。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <!-- “取消”只把 transcriptionVisible 改回 false，不发请求 -->
        <el-button @click="data.transcriptionVisible = false">取消</el-button>
        <!-- “提交转写”调用 submitTranscription，把资料ID和所选语言提交给后端 -->
        <el-button type="primary" :loading="data.submitting" @click="submitTranscription">提交转写</el-button>
      </template>
    </el-dialog>

    <!-- 资料预览弹窗，按文件类型决定用哪种方式呈现 -->
    <!-- destroy-on-close 关闭时销毁内部播放器；@closed 在关闭动画结束后调用 closePreview 释放临时地址 -->
    <el-dialog v-model="data.previewVisible" :title="data.preview.name" width="40%" destroy-on-close @closed="closePreview">
      <!-- 图片直接铺在弹窗里 -->
      <img v-if="data.preview.kind === 'image'" :src="data.preview.url" style="max-width: 100%; display: block; margin: 0 auto" alt="">
      <!-- 视频和音频交给浏览器自带的播放器 -->
      <video v-else-if="data.preview.kind === 'video'" :src="data.preview.url" controls style="width: 100%; max-height: 62vh"></video>
      <audio v-else-if="data.preview.kind === 'audio'" :src="data.preview.url" controls style="width: 100%"></audio>
      <!-- PDF 用 iframe 嵌浏览器自带的阅读器 -->
      <iframe v-else-if="data.preview.kind === 'pdf'" :src="data.preview.url" style="width: 100%; height: 62vh; border: none"></iframe>
      <!-- 纯文本按原样展示，保留换行和缩进 -->
      <pre v-else-if="data.preview.kind === 'text'" style="max-height: 62vh; overflow: auto; margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 13px; line-height: 1.8">{{ data.preview.text }}</pre>
      <!-- Word 文档由后端读出文字，这里按段落还原，标题段落加粗放大 -->
      <div v-else-if="data.preview.kind === 'docx'" style="max-height: 62vh; overflow: auto; line-height: 1.9">
        <div style="margin-bottom: 10px; color: rgba(17, 24, 39, .55); font-size: 13px">
          浏览器无法直接打开 Word 文档，这里展示的是文档中的文字内容，排版和图片请下载后查看
        </div>
        <template v-for="(block, index) in data.preview.blocks" :key="index">
          <div v-if="block.type === 'heading'" style="font-size: 16px; font-weight: 600; color: rgba(17, 24, 39, .95); margin: 16px 0 6px">{{ block.text }}</div>
          <!-- 表格块的第一行作为表头，其余行作为数据行，scope.row[col] 取对应列的单元格文字 -->
          <el-table v-else-if="block.type === 'table'" :data="block.rows.slice(1)" size="small" border style="margin: 8px 0">
            <el-table-column v-for="(head, col) in block.rows[0]" :key="col" :label="head" show-overflow-tooltip>
              <template #default="scope">{{ scope.row[col] }}</template>
            </el-table-column>
          </el-table>
          <div v-else style="color: rgba(17, 24, 39, .75)">{{ block.text }}</div>
        </template>
      </div>
      <!-- 压缩包只列内部文件清单，不解压也不读取里面的内容 -->
      <div v-else-if="data.preview.kind === 'zip'">
        <div style="margin-bottom: 10px; color: rgba(17, 24, 39, .55); font-size: 13px">
          共 {{ data.preview.entries.length }} 个条目，仅展示压缩包内的文件清单
        </div>
        <el-table :data="data.preview.entries" size="small" border max-height="55vh">
          <el-table-column prop="name" label="文件名" show-overflow-tooltip />
          <!-- 目录项没有大小，显示短横线；文件项用 sizeText 把字节数换算成 B、KB、MB、GB -->
          <el-table-column label="大小" width="120" align="right">
            <template #default="scope">{{ scope.row.is_dir ? '-' : sizeText(scope.row.size) }}</template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import request from '@/utils/request.js'
import { meetingLabel } from '@/constants/label.js'
import { assertBlobIsNotError } from '@/utils/blobError.js'
import router from '@/router/index.js'

const route = useRoute()

const data = reactive({
  // 顶部会议下拉框的值；从第 5 章“资料”按钮跳过来时，初值取路由 query.meetingId
  meetingId: Number(route.query.meetingId) || null,
  // 会议下拉框的选项，由 loadMeetings 请求 /meeting/selectOptions 填充
  meetings: [],
  // 文件名输入框，作为 selectPage 的 fileName 参数
  fileName: '',
  // 资料类型下拉框，作为 selectPage 的 fileType 参数
  fileType: '',
  // 当前页码，查询、重置、切换会议时回到 1
  pageNum: 1,
  // 每页条数，随 selectPage 请求提交
  pageSize: 10,
  // selectPage 返回的总条数，为 0 时分页条不渲染
  total: 0,
  // selectPage 返回的当前页资料，表格逐行渲染
  tableData: [],
  // 表格勾选出的资料ID，批量删除时作为请求体提交
  ids: [],
  // 正在上传的文件个数，大于 0 时上传按钮禁用并显示 loading
  uploading: 0,
    uploadPercent: 0,
    uploadController: null,
    uploadName: '',
    submitting: false,
  // 控制“发起转写”弹窗显示
  transcriptionVisible: false,
  // 发起转写弹窗的表单，language 为空字符串表示交给模型自动识别
  transcriptionForm: { materialId: null, fileName: '', language: 'zh' },
  // 控制资料查看弹窗显示
  previewVisible: false,
  // 预览弹窗的数据：kind 决定用哪种方式呈现，url 是本地生成的临时地址
  preview: { kind: '', name: '', url: '', text: '', entries: [], blocks: [] }
})

const allowedExtensions = new Set([
  'mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'wma',
  'mp4', 'mov', 'avi', 'mkv', 'webm', 'mpeg', 'mpg',
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'md',
  'zip', 'rar', '7z', 'png', 'jpg', 'jpeg', 'gif'
])

// 浏览器能直接呈现的格式，按这张表决定弹窗里用 img、video、audio 还是 iframe
const PREVIEW_KINDS = {
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image',
  mp4: 'video', webm: 'video',
  mp3: 'audio', wav: 'audio', m4a: 'audio', aac: 'audio', ogg: 'audio',
  pdf: 'pdf',
  txt: 'text', md: 'text',
  docx: 'docx',
  zip: 'zip'
}

// 取文件名后缀，用来查上面这张表
const extensionOf = (fileName) => (fileName || '').split('.').pop().toLowerCase()

// doc、xls、ppt、rar、7z 这些浏览器打不开、后端也读不出内容，不显示查看入口，让用户直接下载
const canPreview = (row) => !!PREVIEW_KINDS[extensionOf(row.file_name)]

// 把字节数换算成页面上好读的单位
const sizeText = (size) => {
  // 0 字节或空值直接显示 0 B
  if (!size) {
    return '0 B'
  }
  const units = ['B', 'KB', 'MB', 'GB']
  let value = size
  let index = 0
  // 每满 1024 就除一次并换下一个单位，最多换到 GB
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  // 以 B 为单位时不带小数，其余单位保留一位小数
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

// 会议下拉框显示“编号 / 主题”（共享 @/constants/label）
// 把 AUDIO / VIDEO / ATTACHMENT 翻译成中文
const fileTypeText = (type) => ({ AUDIO: '录音', VIDEO: '视频', ATTACHMENT: '附件' }[type] || type)
// 三种类型对应的标签颜色
const fileTypeTag = (type) => ({ AUDIO: 'success', VIDEO: 'warning', ATTACHMENT: 'info' }[type] || '')

const loadMeetings = () => {
  // 下拉选项只返回登录用户有权访问的会议。
  request.get('/meeting/selectOptions').then(res => {
    if (res.code === '200') {
      data.meetings = res.data || []
      // 从第 5 章带过来的 meetingId 可能已经不在可见范围里，先确认它还在
      const selectedExists = data.meetings.some(item => item.id === data.meetingId)
      if (!selectedExists) {
        // 不在就退回第一场会议；一场都没有时保持 null，列表显示为空
        data.meetingId = data.meetings[0]?.id || null
      }
      // 会议确定后才加载资料列表
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
  // 一场会议都没选中时不发请求，直接把表格清空
  if (!data.meetingId) {
    data.tableData = []
    data.total = 0
    return
  }
  // 查询条件与分页参数同时提交给会议资料分页接口。
  request.get('/meetingMaterial/selectPage', {
    params: {
      // 顶部选中的会议ID，后端据此限定数据范围并校验权限
      meetingId: data.meetingId,
      // 文件名输入框的值
      fileName: data.fileName,
      // 资料类型下拉框的值
      fileType: data.fileType,
      pageNum: data.pageNum,
      pageSize: data.pageSize
    }
  }).then(res => {
    if (res.code === '200') {
      data.tableData = res.data?.list || []
      data.total = res.data?.total || 0
    } else {
      ElMessage.error(res.msg)
    }
  })
}

const beforeUpload = (file) => {
  // 没选会议时不知道传到哪场会议下，直接拦住
  if (!data.meetingId) {
    ElMessage.warning('请先选择会议')
    return false
  }
  // 取扩展名并转小写，和白名单比对
  const extension = file.name.includes('.') ? file.name.split('.').pop().toLowerCase() : ''
  if (!allowedExtensions.has(extension)) {
    ElMessage.error('不支持该文件格式')
    return false
  }
  // 和后端 MAX_FILE_SIZE 相同的 500MB 上限，前端先判断一次，超限文件不会发出上传请求
  if (file.size > 500 * 1024 * 1024) {
    ElMessage.error('单个文件不能超过500MB')
    return false
  }
  return true
}

const uploadFile = (options) => {
  // el-upload 选中的文件以 file 字段放入 FormData。
  const formData = new FormData()
  formData.append('file', options.file)
  // 上传计数加一，按钮进入 loading；记录本批次的取消控制器与文件名
  data.uploading += 1
  const controller = new AbortController()
  data.uploadController = controller
  data.uploadName = options.file.name
  // 会议ID拼在地址里，对应后端的路径参数 meeting_id
  request.post('/meetingMaterial/upload/' + data.meetingId, formData, {
    // 大文件上传时间不确定，关掉默认的 30 秒超时
    timeout: 0,
    onUploadProgress: event => {
      if (event.total) {
        // 把已传字节数换算成百分比回传给 el-upload，进度条随之更新
        const percent = Math.round(event.loaded * 100 / event.total)
        options.onProgress({ percent })
        data.uploadPercent = percent
        data.uploadName = options.file.name
      }
    },
    // 取消信号：cancelUpload 调 abort 后请求以 AbortError 结束
    signal: controller.signal
  }).then(res => {
    if (res.code === '200') {
      // 后端识别出内容重复时 duplicated 为 true，提示文案不同
      ElMessage.success(res.data?.duplicated ? '该资料已存在' : '上传成功')
      options.onSuccess(res)
      // 重新拉列表，新上传的文件出现在第一行
      load()
    } else {
      ElMessage.error(res.msg)
      options.onError(new Error(res.msg))
    }
  }).catch(error => {
    if (error.code === 'ERR_ABORTED' || error.name === 'AbortError' || error.code === 'canceled') {
      ElMessage.warning(`已取消上传：${options.file.name}`)
    }
    options.onError(error)
  }).finally(() => {
    // 无论成功失败都把计数减回来，全部结束后按钮才解除 loading
    data.uploading -= 1
    data.uploadPercent = 0
    data.uploadController = null
  })
}

// 取消正在进行的上传（多个并发时全部中止）
const cancelUpload = () => {
  if (data.uploadController) data.uploadController.abort()
}

const preview = (row) => {
  // 按点击行的文件名后缀取出 kind，下面按它分三条路径取数
  const kind = PREVIEW_KINDS[extensionOf(row.file_name)]
  // 重置预览对象：清掉上一次预览留下的地址、文本、清单和段落，name 作为弹窗标题
  data.preview = { kind, name: row.file_name, url: '', text: '', entries: [], blocks: [] }
  if (kind === 'docx') {
    // Word 文档不下载文件本身，由后端读出文字后按段落返回
    request.get('/meetingMaterial/docxPreview/' + row.id).then(res => {
      if (res.code === '200') {
        // 后端 docx_preview 返回的 blocks 数组，每项是 heading、text 或 table
        data.preview.blocks = res.data?.blocks || []
        // 段落数据就位后再打开弹窗，模板进入 kind === 'docx' 分支
        data.previewVisible = true
      } else {
        // 例如“文档已损坏，无法读取内容”
        ElMessage.error(res.msg)
      }
    })
    return
  }
  if (kind === 'zip') {
    // 压缩包不下载文件本身，只向后端要一份内部清单
    request.get('/meetingMaterial/zipEntries/' + row.id).then(res => {
      if (res.code === '200') {
        // 后端 zip_entries 返回的 entries 数组，每项含 name、is_dir、size
        data.preview.entries = res.data?.entries || []
        // 清单就位后打开弹窗，模板进入 kind === 'zip' 分支
        data.previewVisible = true
      } else {
        // 例如“压缩包已损坏，无法读取文件清单”
        ElMessage.error(res.msg)
      }
    })
    return
  }
  // 其余类型走下载接口取 Blob，接口本身带会议权限校验，拿到后在本地生成临时地址
  request.get('/meetingMaterial/download/' + row.id, {
    // 按二进制接收，第 4 章的响应拦截器看到 blob 会原样返回
    responseType: 'blob',
    // 大文件读取时间不确定，关掉默认的 30 秒超时
    timeout: 0
  }).then(async blob => {
    // blob 可能是后端的业务错误 JSON（如"文件不存在"）——先识别，不再黑屏
    blob = await assertBlobIsNotError(blob)
    if (kind === 'text') {
      // 文本直接读成字符串展示，不需要临时地址
      const content = await blob.text()
      // 写入 preview.text，模板中的 pre 标签原样显示
      data.preview.text = content
      data.previewVisible = true
      return
    }
    // 图片、音视频、PDF 生成 blob: 开头的本地地址，交给 img、audio、video、iframe 的 src
    data.preview.url = URL.createObjectURL(blob)
    data.previewVisible = true
  }).catch(error => ElMessage.error(error.message || '预览失败'))
}

// 弹窗关闭后释放临时地址，否则这块内存要等页面刷新才回收
const closePreview = () => {
  // 只有图片、音视频、PDF 生成过 blob 地址，文本、DOCX、ZIP 的 url 为空字符串
  if (data.preview.url) {
    URL.revokeObjectURL(data.preview.url)
  }
  // 把预览对象恢复成空值，下次打开时不会闪现上一份资料的内容
  data.preview = { kind: '', name: '', url: '', text: '', entries: [], blocks: [] }
}

const download = (row) => {
  // 下载接口返回 Blob，页面使用数据库中的原始文件名保存到本地。
  request.get('/meetingMaterial/download/' + row.id, {
    // 告诉 Axios 按二进制接收，第 4 章的响应拦截器看到它会原样返回不做 JSON 解析
    responseType: 'blob',
    // 大文件下载同样关掉超时
    timeout: 0
  }).then(async blob => {
    // 后端错误 JSON 不落成"以原资料名命名的损坏文件"
    blob = await assertBlobIsNotError(blob)
    // 把二进制数据变成一个临时地址
    const url = URL.createObjectURL(blob)
    // 造一个隐藏的 a 标签并触发点击，浏览器按 download 属性的名字保存
    const link = document.createElement('a')
    link.href = url
    link.download = row.file_name
    link.click()
    // 用完立刻释放临时地址
    URL.revokeObjectURL(url)
  })
}

const startTranscription = (row) => {
  // 每次打开都回到默认的中文，避免沿用上一次选的语言
  // materialId 取自点击的资料行，提交时拼进请求地址；fileName 只用于弹窗里展示
  data.transcriptionForm = { materialId: row.id, fileName: row.file_name, language: 'zh' }
  // 打开“发起转写”弹窗
  data.transcriptionVisible = true
}

const submitTranscription = () => {
  // 防重复提交：慢网络下双击会创建两个转写任务
  if (data.submitting) return
  data.submitting = true
  // 资料ID拼进地址 POST /transcription/start/{material_id}，语言作为 JSON 请求体的 language 字段
  request.post('/transcription/start/' + data.transcriptionForm.materialId, {
    // zh、en、ja、ko 或空字符串，后端据此决定是否给模型传语言提示
    language: data.transcriptionForm.language
  }).then(res => {
    if (res.code === '200') {
      ElMessage.success('转写任务已提交')
      // 关闭弹窗
      data.transcriptionVisible = false
      // 提交后直接跳到转写任务页，带上会议ID只看这场会议的任务
      router.push({ path: '/manager/transcription', query: { meetingId: data.meetingId } })
      data.submitting = false
    } else {
      // 例如“请先由管理员启用转写模型配置”“该资料已完成转写”，弹窗保持打开
      ElMessage.error(res.msg)
    }
    data.submitting = false
  })
}

const del = (id) => {
  ElMessageBox.confirm('删除后资料文件无法恢复，您确定删除吗？', '删除确认', { type: 'warning' }).then(() => {
    request.delete('/meetingMaterial/delete/' + id).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        reloadAfterDelete()
      } else {
        // 例如“该资料已关联转写任务，不能删除”
        ElMessage.error(res.msg)
      }
    })
  })
}

const delBatch = () => {
  // 一条都没勾时直接提示，不发请求
  if (!data.ids.length) {
    ElMessage.warning('请选择数据')
    return
  }
  ElMessageBox.confirm('删除后资料文件无法恢复，您确定删除吗？', '删除确认', { type: 'warning' }).then(() => {
    // DELETE 请求通过配置项的 data 字段带上勾选的资料ID数组
    request.delete('/meetingMaterial/deleteBatch', { data: data.ids }).then(res => {
      if (res.code === '200') {
        ElMessage.success('删除成功')
        // 勾选状态已经失效，清空后重新加载
        data.ids = []
        reloadAfterDelete()
      } else {
        ElMessage.error(res.msg)
      }
    })
  })
}

const handleSelectionChange = (rows) => {
  // 勾选变化时保留选中行的ID，供批量删除提交
  data.ids = rows.map(item => item.id)
}

const changeMeeting = () => {
  // 换了会议，页码回到第一页
  data.pageNum = 1
  // 上一场会议的文件名和类型筛选对新会议没有意义，一并清掉
  data.fileName = ''
  data.fileType = ''
  load()
}

const search = () => {
  // 换了查询条件后回到第一页再查
  data.pageNum = 1
  load()
}

const reset = () => {
  // 清空文件名和类型两个条件，会议下拉框保持不变
  data.fileName = ''
  data.fileType = ''
  data.pageNum = 1
  load()
}

loadMeetings()
</script>
