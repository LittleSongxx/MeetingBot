import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

import CitationCard from '@/components/CitationCard.vue'
import type { ChatCitation } from '@/api/chat'

function routerOf() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/manager/transcription', component: { template: '<div />' } },
    ],
  })
}

const citation: ChatCitation = {
  ref: 2,
  kind: 'chunk',
  meeting_id: 17,
  quote: '上线时间定在十月',
  meeting_title: '产品发布评审会议',
  speaker: '张晨',
  start_second: 125,
  authority: 'RAW',
}

describe('CitationCard', () => {
  it('显示编号、出处、原句与权威级', () => {
    const wrapper = mount(CitationCard, {
      props: { citation },
      global: { plugins: [routerOf()] },
    })
    expect(wrapper.text()).toContain('[2]')
    expect(wrapper.text()).toContain('产品发布评审会议 · 张晨 · 02:05')
    expect(wrapper.text()).toContain('上线时间定在十月')
    expect(wrapper.text()).toContain('转写原话')
  })

  it('缺字段时只少显示，不显示拼接痕迹', () => {
    const wrapper = mount(CitationCard, {
      props: { citation: { ref: 1, kind: 'chunk', meeting_id: 5, quote: '只有原句' } },
      global: { plugins: [routerOf()] },
    })
    expect(wrapper.text()).toContain('会议 5')
    expect(wrapper.text()).not.toContain('·  ·')
    expect(wrapper.text()).not.toContain('undefined')
  })

  it('点击跳转到带 meetingId 的转写页（也就是"点回原会议"）', async () => {
    const router = routerOf()
    await router.push('/')
    await router.isReady()
    const wrapper = mount(CitationCard, {
      props: { citation },
      global: { plugins: [router] },
    })
    await wrapper.find('button.citation-link').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/manager/transcription')
    expect(router.currentRoute.value.query.meetingId).toBe('17')
  })

  it('已确认纪要的引用标明"已确认"', () => {
    const wrapper = mount(CitationCard, {
      props: { citation: { ...citation, authority: 'CONFIRMED' } },
      global: { plugins: [routerOf()] },
    })
    expect(wrapper.text()).toContain('已确认纪要')
  })
})
