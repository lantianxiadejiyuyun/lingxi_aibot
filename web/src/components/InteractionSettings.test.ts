import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import InteractionSettings from './InteractionSettings.vue'
import { api, put } from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn()}))
vi.mock('../lib/ui',()=>({notify:vi.fn()}))
const initial=()=>({mode:'natural',configured:false,customized:false,revision:'rev-1',group_count:2,presets:[{mode:'natural',paragraphs:2,followup_delay:15,followup_round:1,followup_daily:3,group_cooldown:180,group_hourly:5},{mode:'quiet',paragraphs:2,followup_delay:20,followup_round:1,followup_daily:2,group_cooldown:600,group_hourly:2}]})
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockResolvedValue(initial());vi.mocked(put).mockResolvedValue({...initial(),configured:true})})
const setup=()=>mount(InteractionSettings,{global:{stubs:{RouterLink:{template:'<a><slot /></a>'}}}})
it('shows three choices and previews their impact without saving on selection',async()=>{
 const w=setup();await flushPromises()
 expect(w.findAll('.interaction-preset')).toHaveLength(3)
 await w.findAll('.interaction-preset')[0]!.trigger('click');expect(put).not.toHaveBeenCalled()
 expect(w.find('.interaction-preview').text()).toContain('至少等 20 分钟')
 expect(w.find('.interaction-preview').text()).toContain('沿用现有开关')
 await w.find('.interaction-preview>.button').trigger('click');await flushPromises()
 expect(put).toHaveBeenCalledWith('/interaction',{mode:'quiet',revision:'rev-1'})
 w.unmount()
})
it('keeps the draft after a conflict and explains existing custom adjustments',async()=>{
 vi.mocked(api).mockResolvedValue({...initial(),configured:true,customized:true})
 vi.mocked(put).mockRejectedValue(new Error('设置刚刚有变化，请刷新'))
 const w=setup();await flushPromises();expect(w.text()).toContain('已有单独调整')
 await w.findAll('.interaction-preset')[0]!.trigger('click')
 await w.find('.interaction-preview>.button').trigger('click');await flushPromises()
 expect(w.find('[role="alert"]').text()).toContain('请刷新')
 expect(w.findAll('.interaction-preset')[0]!.attributes('aria-pressed')).toBe('true');w.unmount()
})
