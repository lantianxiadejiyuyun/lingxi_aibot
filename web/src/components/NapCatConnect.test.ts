import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import NapCatConnect from './NapCatConnect.vue'
import { api, post, session } from '../lib/api'
import { deploymentUrl } from '../lib/deployment'

vi.mock('../lib/api', () => ({ api: vi.fn(), post: vi.fn(), session: { user: { id: 'owner', is_admin: true } } }))
vi.mock('../lib/ui', () => ({ notify: vi.fn() }))
const deployment = {
  public_url: 'http://nas:18081',
  napcat: { managed: true, webui_url: 'http://nas:6099/webui/', http_url: 'http://napcat:3000', callback_url: 'http://nas:18081/api/v2/qq/inbound/owner', internal_callback_url: 'http://app:8000/api/v2/qq/inbound/owner', configure_available: true },
  feishu: { managed: true },
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(post).mockReset(); session.user!.is_admin = true; vi.mocked(api).mockResolvedValue(deployment) })
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

function enableLogin() {
  vi.mocked(api).mockResolvedValue({ ...deployment, napcat: { ...deployment.napcat, login_available: true } })
}

it('requests a session only on click and sends it through an origin-checked handoff without a credential URL', async () => {
  enableLogin()
  const replace = vi.fn(), popup = { opener: window, location: { replace }, close: vi.fn(), postMessage: vi.fn() }
  const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  let complete!: (value: { url: string; credential: string }) => void
  vi.mocked(post).mockReturnValueOnce(new Promise(resolve => { complete = resolve }))
  const wrapper = mount(NapCatConnect); await flushPromises()
  expect(wrapper.find('a').exists()).toBe(false)
  expect(post).not.toHaveBeenCalled()
  expect(open).not.toHaveBeenCalled()
  const button = wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!
  await button.trigger('click')
  expect(button.attributes('disabled')).toBeDefined()
  await button.trigger('click')
  expect(post).toHaveBeenCalledExactlyOnceWith('/deployment/napcat/login')
  expect(open).toHaveBeenCalledExactlyOnceWith('about:blank', '_blank')
  expect(popup.opener).toBe(window)
  const url = 'http://nas:6099/webui/lingxi-login/', credential = 'temporary-test-session'
  complete({ url, credential }); await flushPromises()
  expect(replace).toHaveBeenCalledExactlyOnceWith(`${url}#origin=${encodeURIComponent(window.location.origin)}`)
  expect(popup.postMessage).not.toHaveBeenCalled()
  const nonce = '0123456789abcdef0123456789abcdef'
  const event = (type: string, origin = 'http://nas:6099', source = popup) => new MessageEvent('message', { origin, source: source as unknown as Window, data: { type, nonce } })
  window.dispatchEvent(event('lingxi-napcat-ready', 'https://unexpected.example'))
  window.dispatchEvent(event('lingxi-napcat-ready', 'http://nas:6099', { ...popup }))
  expect(popup.postMessage).not.toHaveBeenCalled()
  window.dispatchEvent(event('lingxi-napcat-ready'))
  window.dispatchEvent(event('lingxi-napcat-ready'))
  expect(popup.postMessage).toHaveBeenCalledExactlyOnceWith({ type: 'lingxi-napcat-session', nonce, credential }, 'http://nas:6099')
  window.dispatchEvent(event('lingxi-napcat-complete')); await flushPromises()
  expect(popup.close).not.toHaveBeenCalled()
  expect(wrapper.html()).not.toContain(credential)
  expect(wrapper.get('input[aria-label="NapCat 管理地址"]').element).toHaveProperty('value', 'http://nas:6099/webui/')
  expect(Object.values(localStorage)).not.toContain(credential)
  expect(Object.values(sessionStorage)).not.toContain(credential)
  wrapper.unmount()
  expect(popup.close).not.toHaveBeenCalled()
})

it('closes the empty window and shows failed login requests', async () => {
  enableLogin()
  const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  vi.mocked(post).mockRejectedValueOnce(new Error('NapCat 尚未准备好，请稍后重试'))
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.trigger('click'); await flushPromises()
  expect(popup.close).toHaveBeenCalledOnce()
  expect(popup.location.replace).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toBe('NapCat 尚未准备好，请稍后重试')
  expect(wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('does not request a session if the browser blocks the new window', async () => {
  enableLogin()
  vi.spyOn(window, 'open').mockReturnValue(null)
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.trigger('click'); await flushPromises()
  expect(post).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toContain('浏览器阻止了新窗口')
  wrapper.unmount()
})

it.each([
  'https://unexpected.example/webui/lingxi-login/',
  'http://nas:6099/webui/lingxi-login/?token=must-not-be-in-query',
  'http://nas:6099/webui/lingxi-login/#credential=must-not-be-in-fragment',
])('refuses untrusted or credential-bearing handoff URLs: %s', async url => {
  enableLogin()
  const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  vi.mocked(post).mockResolvedValueOnce({ url, credential: 'temporary-test-session' })
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.trigger('click'); await flushPromises()
  expect(popup.close).toHaveBeenCalledOnce()
  expect(popup.location.replace).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toBe('NapCat 登录地址无效，请刷新后重试。')
  wrapper.unmount()
})

it('cleans up an unfinished handoff after a timeout', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  enableLogin()
  const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn(), postMessage: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  vi.mocked(post).mockResolvedValueOnce({ url: 'http://nas:6099/webui/lingxi-login/', credential: 'temporary-test-session' })
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.trigger('click'); await flushPromises()
  await vi.advanceTimersByTimeAsync(30_000); await flushPromises()
  expect(popup.close).toHaveBeenCalledOnce()
  expect(wrapper.get('[role="alert"]').text()).toContain('超时')
  window.dispatchEvent(new MessageEvent('message', { origin: 'http://nas:6099', source: popup as unknown as Window, data: { type: 'lingxi-napcat-ready', nonce: '0123456789abcdef0123456789abcdef' } }))
  expect(popup.postMessage).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('cancels an unfinished handoff on unmount', async () => {
  enableLogin()
  const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn(), postMessage: vi.fn() }
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
  vi.mocked(post).mockResolvedValueOnce({ url: 'http://nas:6099/webui/lingxi-login/', credential: 'temporary-test-session' })
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '一键进入 NapCat')!.trigger('click'); await flushPromises()
  wrapper.unmount(); await flushPromises()
  expect(popup.close).toHaveBeenCalled()
  window.dispatchEvent(new MessageEvent('message', { origin: 'http://nas:6099', source: popup as unknown as Window, data: { type: 'lingxi-napcat-ready', nonce: '0123456789abcdef0123456789abcdef' } }))
  expect(popup.postMessage).not.toHaveBeenCalled()
})

it('opens the published management address and configures only after an explicit click', async () => {
  const wrapper = mount(NapCatConnect); await flushPromises()
  expect(wrapper.get('a').attributes('href')).toBe('http://nas:6099/webui/')
  expect(wrapper.get('input[aria-label="HTTP 客户端回调（同一部署内）"]').element).toHaveProperty('value', 'http://app:8000/api/v2/qq/inbound/owner')
  expect(wrapper.text()).not.toContain('连接正常')
  expect(post).not.toHaveBeenCalled()
  await wrapper.findAll('button').find(b => b.text() === '使用内置 NapCat')!.trigger('click'); await flushPromises()
  expect(post).toHaveBeenCalledWith('/deployment/napcat/configure')
  expect(wrapper.emitted('configured')).toHaveLength(1)
  wrapper.unmount()
})

it('keeps configuration failures visible without claiming that settings changed', async () => {
  vi.mocked(post).mockRejectedValueOnce(new Error('仅管理员可配置'))
  const wrapper = mount(NapCatConnect); await flushPromises()
  await wrapper.findAll('button').find(b => b.text() === '使用内置 NapCat')!.trigger('click'); await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toBe('仅管理员可配置')
  expect(wrapper.emitted('configured')).toBeUndefined()
  wrapper.unmount()
})

it('supports an older backend with no deployment endpoint without guessing a management URL', async () => {
  vi.mocked(api).mockRejectedValueOnce(new Error('404'))
  const wrapper = mount(NapCatConnect); await flushPromises()
  expect(wrapper.find('a').exists()).toBe(false)
  expect(wrapper.text()).toContain('尚未设置 NapCat 管理地址')
  expect(wrapper.get('input[aria-label="HTTP 客户端回调（外部 NapCat）"]').element).toHaveProperty('value', `${window.location.origin}/api/v2/qq/inbound/owner`)
  wrapper.unmount()
})

it('hides deployment management and avoids privileged requests for other users', async () => {
  session.user!.is_admin = false
  const wrapper = mount(NapCatConnect); await flushPromises()
  expect(wrapper.find('section').exists()).toBe(false)
  expect(api).not.toHaveBeenCalled()
  expect(post).not.toHaveBeenCalled()
  wrapper.unmount()
})

it('selects the address when copying is unavailable on an HTTP LAN deployment', async () => {
  const wrapper = mount(NapCatConnect); await flushPromises()
  const input = wrapper.get('input[aria-label="NapCat 管理地址"]').element as HTMLInputElement
  const select = vi.spyOn(input, 'select')
  await wrapper.get('button[aria-label="复制NapCat 管理地址"]').trigger('click'); await flushPromises()
  expect(select).toHaveBeenCalled()
  wrapper.unmount()
})

it('rejects unsafe or credential-bearing links and resolves relative callbacks', () => {
  expect(deploymentUrl('javascript:alert(1)')).toBe('')
  expect(deploymentUrl('http://user:secret@nas:6099')).toBe('')
  expect(deploymentUrl('/api/v2/qq/inbound/owner', 'https://bot.example')).toBe('https://bot.example/api/v2/qq/inbound/owner')
})
