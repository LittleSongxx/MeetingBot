// blob 响应可能是后端的业务错误 JSON（如"文件不存在"），直接 createObjectURL
// 会让用户"下载"到一个损坏文件、预览黑屏。三处（预览/下载/导出）统一走这里。
export interface BlobError extends Error {
  /** 后端原始错误信封，调用方可据此判断错误类型 */
  body?: { code?: string; msg?: string }
}

export async function assertBlobIsNotError(blob: Blob | null | undefined): Promise<Blob> {
  if (!blob || !blob.type || !blob.type.includes('application/json')) return blob as Blob
  // JSON 错误体通常很小，整体读出来解析
  const text = await blob.text()
  try {
    const body = JSON.parse(text) as { code?: string; msg?: string }
    if (body && body.code && body.code !== '200') {
      const error: BlobError = new Error(body.msg || '文件获取失败')
      error.body = body
      throw error
    }
  } catch (error) {
    if ((error as BlobError).body) throw error
    // 非 JSON 文本：按原样返回，交给调用方
  }
  return blob
}
