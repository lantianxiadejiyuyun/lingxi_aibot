import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import MoodSettings from './MoodSettings.vue'
import MoodPanel from './MoodPanel.vue'
import { api, put, patch, post } from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),patch:vi.fn(),post:vi.fn()}))
vi.mock('../lib/ui',()=>({notify:vi.fn(),date:()=> '现在',run:async(f:()=>Promise<unknown>)=>f(),confirmAction:async(_a:string,_b:string,f:()=>Promise<unknown>)=>f()}))
const config={enabled:true,profile_id:null,baseline:65,half_life_hours:12,emotion_half_life_hours:1.5,confidence_threshold:0.75,min_interval_secs:30,daily_judgments:80,daily_injections:6}
const state=()=>({allowed:true,state:{value:65,emotions:{happy:12},revision:4,enabled:true,last_error:''},events:[]})
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async path=>path==='/mood'?{config,scopes:[],allowed_private:false,allowed_group:false}:state());vi.mocked(patch).mockResolvedValue({...state(),state:{...state().state,enabled:false,revision:5}})})
it('saves a dedicated preset with complete mood limits',async()=>{
 const w=mount(MoodSettings,{props:{profiles:[{id:'judge',name:'判断模型'} as any]},global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.get('select').setValue('judge');await w.get('form').trigger('submit');await flushPromises()
 expect(put).toHaveBeenCalledWith('/mood',{...config,profile_id:'judge'});expect(w.text()).toContain('个人权限：未开启');w.unmount()
})
it('toggles only the requested group with its latest revision',async()=>{
 const w=mount(MoodPanel,{props:{scope:'group:12345'},global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.get('input[type=checkbox]').setValue(false);await flushPromises()
 expect(patch).toHaveBeenCalledWith('/mood/group%3A12345',{enabled:false,revision:4});
 expect((w.get('input').element as HTMLInputElement).checked).toBe(false);w.unmount()
})
it('reset uses the displayed conversation and refreshes its state',async()=>{
 const w=mount(MoodPanel,{props:{scope:'private:abc'},global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.findAll('button').find(b=>b.text()==='回到平稳')!.trigger('click');await flushPromises()
 expect(post).toHaveBeenCalledWith('/mood/private%3Aabc/reset');expect(api).toHaveBeenCalledTimes(2);w.unmount()
})
it('ignores a stale response when switching conversations',async()=>{
 let finish:(value:any)=>void=()=>{};vi.mocked(api).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 const w=mount(MoodPanel,{props:{scope:'private:old'},global:{stubs:{RouterLink:true}}});await w.setProps({scope:'group:new'});await flushPromises()
 finish({...state(),state:{...state().state,value:99}});await flushPromises();expect(w.get('.mood-value').text()).toBe('65/ 100');w.unmount()
})
