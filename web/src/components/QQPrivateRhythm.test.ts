import {mount,flushPromises} from '@vue/test-utils'
import {expect,it,vi} from 'vitest'
import QQPrivateRhythm from './QQPrivateRhythm.vue'
import {api,put} from '../lib/api'
import {openForm} from '../lib/ui'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn()}))
vi.mock('../lib/ui',()=>({choice:vi.fn(),field:vi.fn(),openForm:vi.fn(),notify:vi.fn(),run:(fn:()=>Promise<void>)=>fn()}))
it('only updates private rhythm and retains shared connection and group settings',async()=>{
 vi.mocked(api).mockResolvedValue({search:{url:'https://search'},qq:{private_enabled:false,onebot:{token:'********'},groups:['30001'],group_settings:{'30001':{proactive:{enabled:true}}}}})
 const w=mount(QQPrivateRhythm,{global:{stubs:{RouterLink:true}}});await w.find('button').trigger('click');await flushPromises()
 await vi.mocked(openForm).mock.calls[0]![0].save({split:'true',max_messages:'2',interval_secs:'1',followup:'true',delay_minutes:'5',max_followups:'3',followup_daily:'6'})
 expect(put).toHaveBeenCalledWith('/integrations',expect.objectContaining({search:{url:'https://search'},qq:expect.objectContaining({private_enabled:false,groups:['30001'],onebot:{token:'********'},group_settings:{'30001':{proactive:{enabled:true}}},followup:{enabled:true,delay_minutes:5,max_followups:3,daily_limit:6}})}))
 expect(w.emitted('changed')).toHaveLength(1);w.unmount()
})
