import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import Bilibili from './Bilibili.vue'
import { api, post } from '../lib/api'
vi.mock('../lib/api', () => ({ api: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), remove: vi.fn(), requestId: () => 'request-key' }))
vi.mock('../lib/ui', () => ({ choice: vi.fn(), field: vi.fn(), openForm: vi.fn(), confirmAction: vi.fn(), notify: vi.fn(), date: () => '10-22 19:00', run: (f: () => Promise<unknown>) => f() }))
const initial = () => ({ config: { enabled: false, keywords: [], daily_at: '19:00', daily_limit: 6, per_run: 3, recent_days: 7, profile_id: null, taste: '' }, account: { connected: false, name: '' }, items: [], total: 0, page: 1, timezone: 'Asia/Shanghai', next_run_at: 0, stats: { total: 0, completed: 0, favorites: 0, today: 0 }, runs: [] })
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockResolvedValue(initial()); vi.mocked(post).mockResolvedValue({ id: 'run' }) })
function setup() { return mount(Bilibili, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } } }) }
it('preserves failed video input and reports a real error instead of showing success', async () => {
  const w = setup(); await flushPromises(); await w.find('#bili-video').setValue('BV1xx411c7mD')
  vi.mocked(post).mockRejectedValueOnce(new Error('请先在设置中添加模型配置'))
  await w.find('.bili-submit form').trigger('submit'); await flushPromises()
  expect((w.find('#bili-video').element as HTMLInputElement).value).toBe('BV1xx411c7mD')
  expect(w.find('[role="alert"]').text()).toContain('添加模型配置')
  expect(post).toHaveBeenCalledWith('/bilibili/run', { video: 'BV1xx411c7mD', retry: false, request_id: 'request-key' }); w.unmount()
})
it('prevents another submission while a durable reading run is active', async () => {
  vi.mocked(api).mockResolvedValue({ ...initial(), runs: [{ id: 'r', job_id: 'j', status: 'running', progress: '正在阅读第 1 条', report: '', created_at: 1 }] })
  const w = setup(); await flushPromises(); await w.find('#bili-video').setValue('BV1xx411c7mD')
  expect(w.find('.bili-submit form button').attributes('disabled')).toBeDefined()
  expect(w.find('.bili-progress').text()).toContain('正在阅读第 1 条')
  await w.find('.bili-progress button').trigger('click'); await flushPromises(); expect(post).toHaveBeenCalledWith('/jobs/j/cancel'); w.unmount()
})
it('shows missing subtitles honestly and filters the requested history', async () => {
  vi.mocked(api).mockResolvedValue({ ...initial(), total: 1, items: [{ id: 'e', title: '测试视频', bvid: 'BV1xx411c7mD', page: 1, author: 'UP主', duration: 10, status: 'skipped', reason: '当前没有取得字幕', analysis: {}, favorite: false }] })
  const w = setup(); await flushPromises(); expect(w.find('.bili-card').text()).toContain('没有字幕'); expect(w.find('.bili-card').text()).not.toContain('已读字幕')
  await w.find('[aria-label="筛选阅读状态"]').setValue('skipped'); await flushPromises(); expect(api).toHaveBeenLastCalledWith('/bilibili?q=&status=skipped&favorite=false&page=1'); w.unmount()
})
