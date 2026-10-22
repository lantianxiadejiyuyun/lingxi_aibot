import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'
import App from './App.vue'
import { session, setSession } from './lib/api'

beforeEach(() => setSession('saved-token', null))
afterEach(() => { vi.unstubAllGlobals(); setSession('', null) })
async function setup() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { template: '<p>工作空间内容</p>' } }] })
  await router.push('/dashboard'); await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router], stubs: { FormModal: true, Login: { template: '<div data-testid="login">登录</div>' } } } })
  await flushPromises()
  return wrapper
}
it('preserves login after a temporary failure and reconnects with the saved token', async () => {
  const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, data: { id: 'owner', username: 'review', timezone: 'Asia/Shanghai' } })))
  vi.stubGlobal('fetch', fetcher)
  const w = await setup()
  expect(w.find('[role="alert"]').text()).toContain('登录信息已保留')
  expect(w.find('[data-testid="login"]').exists()).toBe(false)
  expect(sessionStorage.getItem('lingxi_token')).toBe('saved-token')
  await w.find('.boot-error button').trigger('click'); await flushPromises()
  expect(w.find('.workspace-shell').exists()).toBe(true)
  expect(fetcher.mock.calls[1]![1].headers.Authorization).toBe('Bearer saved-token')
  w.unmount()
})
it('clears an expired session and returns to login on 401', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: false, error: '登录已过期' }), { status: 401 })))
  const w = await setup()
  expect(session.token).toBe('')
  expect(w.find('[data-testid="login"]').exists()).toBe(true)
  expect(w.find('.boot-error').exists()).toBe(false)
  w.unmount()
})
it('keeps a valid session available when the server returns 503', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Service unavailable', { status: 503 })))
  const w = await setup()
  expect(w.find('.boot-error').text()).toContain('503')
  expect(session.token).toBe('saved-token')
  w.unmount()
})
