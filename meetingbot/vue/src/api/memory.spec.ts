import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: mocks }))

import {
  addPreference,
  confirmMemory,
  expireMemory,
  rejectMemory,
  selectMemoryPage,
  updateMemory,
} from '@/api/memory'

const ok = (data: unknown = null) => Promise.resolve({ code: '200', msg: 'ok', data })

describe('memory api', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('列表查询把状态/作用域/类型串进参数', async () => {
    mocks.get.mockResolvedValue({ code: '200', msg: 'ok', data: { total: 0, list: [] } })
    await selectMemoryPage({
      status: 'PROPOSED',
      scope_type: 'SERIES',
      scope_id: 3,
      kind: 'DECISION',
      pageSize: 20,
    })
    expect(mocks.get).toHaveBeenCalledWith('/memory/selectPage', {
      params: {
        status: 'PROPOSED',
        scope_type: 'SERIES',
        scope_id: 3,
        kind: 'DECISION',
        page_num: 1,
        page_size: 20,
      },
    })
  })

  it('确认候选时可以指定被替代的旧记忆', async () => {
    mocks.post.mockReturnValue(ok())
    await confirmMemory(8, 4)
    expect(mocks.post).toHaveBeenCalledWith('/memory/confirm', { id: 8, supersede_id: 4 })
  })

  it('不带替代目标时 supersede_id 为 undefined（不替代任何旧记忆）', async () => {
    mocks.post.mockReturnValue(ok())
    await confirmMemory(8)
    expect(mocks.post).toHaveBeenCalledWith('/memory/confirm', {
      id: 8,
      supersede_id: undefined,
    })
  })

  it('拒绝/过期/编辑/偏好各自打到对应接口', async () => {
    mocks.post.mockReturnValue(ok())
    mocks.put.mockReturnValue(ok())
    await rejectMemory(8)
    await expireMemory(8)
    await updateMemory(8, '新内容')
    await addPreference('回答先给结论')
    expect(mocks.post).toHaveBeenCalledWith('/memory/reject', { id: 8 })
    expect(mocks.post).toHaveBeenCalledWith('/memory/expire', { id: 8 })
    expect(mocks.put).toHaveBeenCalledWith('/memory/update', { id: 8, content: '新内容' })
    expect(mocks.post).toHaveBeenCalledWith('/memory/preference', { content: '回答先给结论' })
  })
})
