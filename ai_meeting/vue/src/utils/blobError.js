// blob 响应可能是后端的业务错误 JSON（如"文件不存在"），直接 createObjectURL
// 会让用户"下载"到一个损坏文件、预览黑屏。三处（预览/下载/导出）统一走这里。
export async function assertBlobIsNotError(blob) {
  if (!blob || !blob.type || !blob.type.includes('application/json')) return blob
  // JSON 错误体通常很小，整体读出来解析
  const text = await blob.text()
  try {
    const body = JSON.parse(text)
    if (body && body.code && body.code !== '200') {
      const error = new Error(body.msg || '文件获取失败')
      error.body = body
      throw error
    }
  } catch (error) {
    if (error.body) throw error
    // 非 JSON 文本：按原样返回，交给调用方
  }
  return blob
}
