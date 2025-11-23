import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/utils/request', () => ({ default: mocks }))

import {
  addModelRoute,
  deleteModelRoute,
  listAiModelOptions,
  listModelRoutes,
  updateModelRoute,
} from '@/api/aiModel'

const ok = (data: unknown = null) => Promise.resolve({ code: '200', msg: 'ok', data })

describe('ai-model routes api', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('路由列表是 GET /aiModel/routes', async () => {
    mocks.get.mockReturnValue(ok([]))
    await listModelRoutes()
    expect(mocks.get).toHaveBeenCalledWith('/aiModel/routes')
  })

  it('新增路由 POST 同路径并带上模型配置', async () => {
    mocks.post.mockReturnValue(ok())
    await addModelRoute({ route_key: 'CHAT_SUMMARY', model_config_id: 3, enabled: true })
    expect(mocks.post).toHaveBeenCalledWith('/aiModel/routes', {
      route_key: 'CHAT_SUMMARY',
      model_config_id: 3,
      enabled: true,
    })
  })

  it('编辑路由 PUT 到带 id 的路径', async () => {
    mocks.put.mockReturnValue(ok())
    await updateModelRoute(4, {
      route_key: 'CHAT',
      model_config_id: 2,
      enabled: false,
    })
    expect(mocks.put).toHaveBeenCalledWith('/aiModel/routes/4', {
      route_key: 'CHAT',
      model_config_id: 2,
      enabled: false,
    })
  })

  it('删除路由地址带 id', async () => {
    mocks.delete.mockReturnValue(ok())
    await deleteModelRoute(4)
    expect(mocks.delete).toHaveBeenCalledWith('/aiModel/routes/4')
  })

  it('模型配置下拉复用 selectPage（一次取 200 条）', async () => {
    mocks.get.mockReturnValue(ok({ total: 0, list: [] }))
    await listAiModelOptions()
    expect(mocks.get).toHaveBeenCalledWith('/aiModel/selectPage', {
      params: { page_num: 1, page_size: 200 },
    })
  })
})
