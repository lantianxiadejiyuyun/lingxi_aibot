import {mount,flushPromises} from '@vue/test-utils'
import {expect,it,vi} from 'vitest'
import FeishuConnect from './FeishuConnect.vue'
import {api,post,put} from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),post:vi.fn(),put:vi.fn(),remove:vi.fn()}))
vi.mock('../lib/ui',()=>({confirmAction:vi.fn(),date:(n:number)=>String(n),field:vi.fn(),notify:vi.fn(),openForm:vi.fn()}))
const initial={enabled:true,configured:true,bound:false,state:'connected',sdk_version:'1.76.0',heartbeat_at:100,spool_count:0,profile_id:null,conversation_id:null,events:[]}
it('generates an explicit private binding command and diagnoses without claiming delivery',async()=>{
 vi.mocked(api).mockImplementation(async p=>p==='/profiles'?[]:{...initial})
 vi.mocked(post).mockImplementation(async p=>p==='/feishu/pair'?{command:'/绑定 one-time-code',expires_at:123}:{bot_enabled:true})
 const w=mount(FeishuConnect,{global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.findAll('button').find(b=>b.text()==='生成绑定码')!.trigger('click');await flushPromises()
 expect(w.find('input[aria-label="飞书绑定命令"]').element).toHaveProperty('value','/绑定 one-time-code')
 expect(w.text()).toContain('私聊发给飞书机器人')
 await w.findAll('button').find(b=>b.text()==='检查应用连接')!.trigger('click');await flushPromises()
 expect(w.text()).toContain('接收与发送权限仍需通过实际私聊验证')
 w.unmount()
})
it('refreshes connection state without discarding unsaved settings',async()=>{
 vi.useFakeTimers()
 vi.mocked(api).mockImplementation(async p=>p==='/profiles'?[]:{...initial})
 const w=mount(FeishuConnect,{global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.find('input[type="checkbox"]').setValue(false)
 await vi.advanceTimersByTimeAsync(15000);await flushPromises()
 expect((w.find('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(false)
 await w.find('form').trigger('submit');await flushPromises()
 expect(put).toHaveBeenCalledWith('/feishu',{enabled:false,profile_id:null})
 w.unmount();vi.useRealTimers()
})
