import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import Companion from './Companion.vue'
import { api, put } from '../lib/api'

vi.mock('../lib/api', () => ({ api: vi.fn(), put: vi.fn(), requestId: () => 'qa-request', session: { user: {id:'owner'} } }))
vi.mock('../lib/ui', () => ({ choice: vi.fn(), confirmAction: vi.fn(), date: () => '', field: vi.fn(), notify: vi.fn(), openForm: vi.fn(), run: vi.fn(action => action()) }))
afterEach(() => vi.clearAllMocks())
function setup(settingsMode=true) {
 const config={enabled:false,active_start:'09:00',active_end:'22:00',quiet_start:'23:00',quiet_end:'08:00',min_interval_minutes:180,daily_limit:3,chat_cooldown_minutes:45,channels:['inapp'],interests:[],share_bilibili:false,profile_id:null}
 vi.mocked(api).mockImplementation(async path => path === '/profiles' ? [] : path === '/integrations' ? {} : {config,activities:[],timezone:'Asia/Shanghai',state:{next_check_at:100},channels:{inapp:true},astrbot:{bound:false,session:''}})
 return mount(Companion, {props:{settingsMode},global:{stubs:{FeishuConnect:true,NapCatConnect:true,RouterLink:{template:'<a><slot /></a>'}}}})
}
it('retains edited settings and displays an error when saving fails', async () => {
 const wrapper=setup();await flushPromises()
 await wrapper.find('textarea').setValue('摄影，猫咪')
 await wrapper.find('input[type="time"]').setValue('10:30')
 vi.mocked(put).mockRejectedValueOnce(new Error('模型未配置'))
 await wrapper.find('form').trigger('submit');await flushPromises()
 expect(wrapper.find('[role="alert"]').text()).toBe('模型未配置')
 expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('摄影，猫咪')
 expect((wrapper.find('input[type="time"]').element as HTMLInputElement).value).toBe('10:30')
 expect(put).toHaveBeenCalledWith('/companion',expect.objectContaining({interests:['摄影','猫咪'],active_start:'10:30'}))
 wrapper.unmount()
})
it('does not save or start a check merely by opening the page or changing a switch', async () => {
 const wrapper=setup();await flushPromises()
 await wrapper.find('.companion-toggle input').setValue(true)
 expect(put).not.toHaveBeenCalled()
 expect(vi.mocked(api).mock.calls.map(args=>args[0]).sort()).toEqual(['/companion','/profiles'])
 expect(wrapper.text()).toContain('保存后按计划运行，不会立即发消息。')
 wrapper.unmount()
})
it('keeps the activity page focused on records with a settings link', async () => {
 const wrapper=setup(false);await flushPromises()
 expect(wrapper.find('form').exists()).toBe(false)
 expect(wrapper.text()).toContain('陪伴设置')
 expect(wrapper.text()).toContain('最近的小记')
 expect(vi.mocked(api).mock.calls.map(args=>args[0])).toEqual(['/companion'])
 expect(put).not.toHaveBeenCalled();wrapper.unmount()
})
