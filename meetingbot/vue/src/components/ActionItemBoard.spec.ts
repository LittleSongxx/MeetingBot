import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ActionItemBoard from '@/components/ActionItemBoard.vue'
import type { ActionItem } from '@/api/actionItem'

function itemOf(patch: Partial<ActionItem> & { id: number; status: string }): ActionItem {
  return {
    series_id: 3,
    series_name: '产品周会',
    meeting_id: 1,
    meeting_title: '产品周会',
    minutes_id: null,
    content: '完成压测报告',
    owner_user_id: 1,
    owner_name: '张晨',
    deadline: null,
    deadline_text: null,
    evidence_json: null,
    last_seen_meeting_id: null,
    update_time: '2026-10-19 10:00:00',
    ...patch,
  }
}

const items: ActionItem[] = [
  itemOf({
    id: 1,
    status: 'OPEN',
    // 2000 年早就过期了：用于断言"已逾期"标记
    deadline: '2000-01-01 00:00:00',
    last_seen_meeting_id: 2,
  }),
  itemOf({ id: 2, status: 'OPEN', content: '补齐接口文档', owner_name: '李雷' }),
  itemOf({ id: 3, status: 'BLOCKED', content: '等待法务确认' }),
  itemOf({ id: 4, status: 'DONE', content: '已上线' }),
  itemOf({ id: 5, status: 'CANCELLED', content: '需求取消' }),
]

describe('ActionItemBoard', () => {
  it('按状态分成四列，列头计数正确', () => {
    const wrapper = mount(ActionItemBoard, { props: { items } })
    const columns = wrapper.findAll('.board-column')
    expect(columns.length).toBe(4)
    expect(columns[0].text()).toContain('待处理')
    expect(columns[0].findAll('.board-card').length).toBe(2)
    expect(columns[1].findAll('.board-card').length).toBe(1)
    expect(columns[2].findAll('.board-card').length).toBe(1)
    expect(columns[3].findAll('.board-card').length).toBe(1)
  })

  it('逾期卡片显示"已逾期"，结转卡片显示来源会议', () => {
    const wrapper = mount(ActionItemBoard, { props: { items } })
    expect(wrapper.text()).toContain('已逾期')
    expect(wrapper.text()).toContain('结转自会议 2')
  })

  it('点击卡片抛出 select 事件（带整条待办）', async () => {
    const wrapper = mount(ActionItemBoard, { props: { items } })
    await wrapper.findAll('.board-card')[0].trigger('click')
    const emitted = wrapper.emitted('select')
    expect(emitted?.length).toBe(1)
    expect(emitted?.[0]?.[0]).toMatchObject({ id: 1, status: 'OPEN' })
  })

  it('加载中显示占位而不是"暂无"', () => {
    const wrapper = mount(ActionItemBoard, { props: { items: [], loading: true } })
    expect(wrapper.text()).toContain('加载中')
    expect(wrapper.findAll('.board-card').length).toBe(0)
  })

  it('空看板每列显示"暂无"', () => {
    const wrapper = mount(ActionItemBoard, { props: { items: [] } })
    expect(wrapper.findAll('.board-empty').length).toBe(4)
  })
})
