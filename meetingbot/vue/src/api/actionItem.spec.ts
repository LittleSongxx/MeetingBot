import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: mocks }))

import {
  listActionItemEvents,
  selectActionItemPage,
  updateActionItemStatus,
} from '@/api/actionItem'

const ok = (data: unknown = null) => Promise.resolve({ code: '200', msg: 'ok', data })

describe('action-item api', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('列表查询带上系列/负责人/状态/逾期筛选', async () => {
    mocks.get.mockResolvedValue({ code: '200', msg: 'ok', data: { total: 0, list: [] } })
    await selectActionItemPage({
      series_id: 3,
      owner_user_id: 11,
      status: 'OPEN',
      overdue: true,
      pageNum: 2,
      pageSize: 10,
    })
    expect(mocks.get).toHaveBeenCalledWith('/action-item/selectPage', {
      params: {
        series_id: 3,
        owner_user_id: 11,
        status: 'OPEN',
        overdue: true,
        page_num: 2,
        page_size: 10,
      },
    })
  })

  it('不勾选逾期时不传 overdue（让后端按"全部"处理）', async () => {
    mocks.get.mockResolvedValue({ code: '200', msg: 'ok', data: { total: 0, list: [] } })
    await selectActionItemPage({ overdue: false })
    const params = mocks.get.mock.calls[0][1].params
    expect(params.overdue).toBeUndefined()
  })

  it('状态更新提交 id/status/note（备注用于 HITL 留痕）', async () => {
    mocks.put.mockReturnValue(ok())
    await updateActionItemStatus({ id: 5, status: 'DONE', note: '已上线，验收通过' })
    expect(mocks.put).toHaveBeenCalledWith('/action-item/updateStatus', {
      id: 5,
      status: 'DONE',
      note: '已上线，验收通过',
    })
  })

  it('事件历史地址带待办 id', async () => {
    mocks.get.mockReturnValue(ok([]))
    await listActionItemEvents(5)
    expect(mocks.get).toHaveBeenCalledWith('/action-item/events/5')
  })
})
