import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

import StateCitationCard from '@/components/StateCitationCard.vue'
import type { ChatCitation } from '@/api/chat'

function routerOf() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/manager/actionItems', component: { template: '<div />' } },
      { path: '/manager/memory', component: { template: '<div />' } },
      { path: '/manager/transcription', component: { template: '<div />' } },
    ],
  })
}

const actionCitation: ChatCitation = {
  ref: 2,
  kind: 'state',
  meeting_id: 17,
  quote: '上线前完成压测',
  meeting_title: '产品发布评审会议',
  state_type: 'ACTION_ITEM',
  state_id: 9,
  status: 'OPEN',
  owner_name: '张晨',
  deadline_text: '下周五前',
  series_name: '产品发布周会',
  source_text: '上线前完成压测并出报告',
}

describe('StateCitationCard', () => {
  it('展示类型、状态、负责人、截止时间、系列与完整正文', () => {
    const wrapper = mount(StateCitationCard, {
      props: { citation: actionCitation },
      global: { plugins: [routerOf()] },
    })
    const text = wrapper.text()
    expect(text).toContain('[2]')
    expect(text).toContain('待办')
    expect(text).toContain('待处理')
    expect(text).toContain('负责人：张晨')
    expect(text).toContain('截止：下周五前')
    expect(text).toContain('产品发布周会')
    expect(text).toContain('上线前完成压测并出报告')
    expect(text).toContain('来源：产品发布评审会议')
  })

  it('点击"查看待办"跳到待办页并带上 state_id', async () => {
    const router = routerOf()
    await router.push('/')
    await router.isReady()
    const wrapper = mount(StateCitationCard, {
      props: { citation: actionCitation },
      global: { plugins: [router] },
    })
    const button = wrapper.findAll('button').find((item) => item.text().includes('查看待办'))
    if (!button) throw new Error('没有渲染"查看待办"按钮')
    await button.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/manager/actionItems')
    expect(router.currentRoute.value.query.actionItemId).toBe('9')
  })

  it('决策类引用展示"决策/已确认"，并跳到记忆页', async () => {
    const router = routerOf()
    await router.push('/')
    await router.isReady()
    const citation: ChatCitation = {
      ...actionCitation,
      state_type: 'DECISION',
      state_id: 4,
      status: 'CONFIRMED',
      owner_name: null,
      deadline_text: null,
    }
    const wrapper = mount(StateCitationCard, {
      props: { citation },
      global: { plugins: [router] },
    })
    expect(wrapper.text()).toContain('决策')
    expect(wrapper.text()).toContain('已确认')
    const button = wrapper.findAll('button').find((item) => item.text().includes('查看记忆'))
    if (!button) throw new Error('没有渲染"查看记忆"按钮')
    await button.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/manager/memory')
    expect(router.currentRoute.value.query.memoryId).toBe('4')
  })

  it('"会议原话"跳转转写页并带 meetingId', async () => {
    const router = routerOf()
    await router.push('/')
    await router.isReady()
    const wrapper = mount(StateCitationCard, {
      props: { citation: actionCitation },
      global: { plugins: [router] },
    })
    const button = wrapper.findAll('button').find((item) => item.text().includes('会议原话'))
    if (!button) throw new Error('没有渲染"会议原话"按钮')
    await button.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/manager/transcription')
    expect(router.currentRoute.value.query.meetingId).toBe('17')
  })
})
