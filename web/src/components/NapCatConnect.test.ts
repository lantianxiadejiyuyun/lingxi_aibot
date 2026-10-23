import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
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
beforeEach(() => { vi.clearAllMocks(); session.user!.is_admin = true; vi.mocked(api).mockResolvedValue(deployment) })

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
