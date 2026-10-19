import { beforeEach, describe, expect, it, vi } from 'vitest'

// 把 axios 实例换成纯 mock：这里验证的是"页面调 api 模块时发出的 URL/参数/载荷"，
// 不需要真的网络。用 vi.hoisted 是因为 vi.mock 的工厂在模块加载前执行。
const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: mocks }))

import {
  archiveSeries,
  assignMeetingToSeries,
  getSeriesDetail,
  selectSeriesPage,
  suggestSeries,
} from '@/api/series'

const ok = (data: unknown = null) => Promise.resolve({ code: '200', msg: 'ok', data })

describe('series api', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('列表查询把筛选条件串成 snake_case 分页参数', async () => {
    mocks.get.mockResolvedValue({ code: '200', msg: 'ok', data: { total: 0, list: [] } })
    await selectSeriesPage({ name: '产品周会', status: 'ACTIVE', pageNum: 2, pageSize: 20 })
    expect(mocks.get).toHaveBeenCalledWith('/meeting/series/selectPage', {
      params: {
        name: '产品周会',
        status: 'ACTIVE',
        cadence: undefined,
        page_num: 2,
        page_size: 20,
      },
    })
  })

  it('列表查询默认第一页十条，空筛选不拼参数', async () => {
    mocks.get.mockResolvedValue({ code: '200', msg: 'ok', data: { total: 0, list: [] } })
    await selectSeriesPage()
    expect(mocks.get).toHaveBeenCalledWith('/meeting/series/selectPage', {
      params: {
        name: undefined,
        status: undefined,
        cadence: undefined,
        page_num: 1,
        page_size: 10,
      },
    })
  })

  it('详情地址带系列 id', async () => {
    mocks.get.mockReturnValue(ok())
    await getSeriesDetail(7)
    expect(mocks.get).toHaveBeenCalledWith('/meeting/series/selectById/7')
  })

  it('归档用 POST + body id', async () => {
    mocks.post.mockReturnValue(ok())
    await archiveSeries(7)
    expect(mocks.post).toHaveBeenCalledWith('/meeting/series/archive', { id: 7 })
  })

  it('建议系列只提交 meeting_id', async () => {
    mocks.post.mockReturnValue(ok({ candidates: [] }))
    await suggestSeries(9)
    expect(mocks.post).toHaveBeenCalledWith('/meeting/series/suggest', { meeting_id: 9 })
  })

  it('归入系列走独立 assign 接口（不受会议状态限制）', async () => {
    mocks.post.mockReturnValue(ok({ meeting_id: 9, series_id: 3, series_name: '周会' }))
    await assignMeetingToSeries(9, 3)
    expect(mocks.post).toHaveBeenCalledWith('/meeting/series/assign', {
      meeting_id: 9,
      series_id: 3,
    })
  })

  it('移出系列传 null', async () => {
    mocks.post.mockReturnValue(ok({ meeting_id: 9, series_id: null, series_name: null }))
    await assignMeetingToSeries(9, null)
    expect(mocks.post).toHaveBeenCalledWith('/meeting/series/assign', {
      meeting_id: 9,
      series_id: null,
    })
  })
})
