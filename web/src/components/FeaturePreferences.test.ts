import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import FeaturePreferences from './FeaturePreferences.vue'
import { api, put } from '../lib/api'
import { openForm } from '../lib/ui'

vi.mock('../lib/api', () => ({ api: vi.fn(), put: vi.fn() }))
vi.mock('../lib/ui', async () => ({ ...await vi.importActual('../lib/ui'), openForm: vi.fn(), notify: vi.fn() }))
const config = () => ({ enabled: false, autonomous: false, daily_at: '19:00', keywords: [], daily_limit: 6, per_run: 3, recent_days: 7, profile_id: 'reader', taste: '有趣的日常' })
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/profiles') return [{ id: 'reader', name: '阅读预设' }] as any
    if (path === '/bilibili') return { config: config(), timezone: 'Asia/Shanghai' } as any
    return { settings: {} } as any
  })
})
it('saves autonomous selection with optional interests and preserves the schedule and chosen profile', async () => {
  const w = mount(FeaturePreferences, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } } }); await flushPromises()
  await w.findAll('button').find(b => b.text().includes('配置阅读安排'))!.trigger('click'); await flushPromises()
  const form = vi.mocked(openForm).mock.calls[0]![0]
  const mode = form.fields!.find(f => f.key === 'autonomous')!
  expect(mode.options).toEqual([['true', 'AI 自己决定想看什么'], ['false', '按我填写的固定兴趣发现']])
  expect(mode.value).toBe(false)
  expect(form.fields!.find(f => f.key === 'keywords')!.required).not.toBe(true)
  const values = Object.fromEntries(form.fields!.map(f => [f.key, String(f.value)]))
  await form.save({ ...values, autonomous: 'true', keywords: '' })
  expect(put).toHaveBeenCalledWith('/bilibili', { ...config(), autonomous: true })
  w.unmount()
})
