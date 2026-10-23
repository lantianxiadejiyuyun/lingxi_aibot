import { mount,flushPromises } from '@vue/test-utils'
import { expect,it,vi,beforeEach,afterEach } from 'vitest'
import Persona from './Persona.vue'
import {api,put,post} from '../lib/api'
import {openForm} from '../lib/ui'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),post:vi.fn(),remove:vi.fn(),requestId:()=> 'manual-test'}))
vi.mock('../lib/ui',()=>({choice:vi.fn(),field:vi.fn(),openForm:vi.fn(),confirmAction:vi.fn(),date:()=>'',notify:vi.fn(),run:(fn:()=>Promise<void>)=>fn()}))
const growth={enabled:false,revision:2,turns:0,days:0,pending:0,last_reflected_at:null,last_error:'',traits:[],job_status:null,can_reflect:true,batch_size:6,max_per_run:24}
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async(path:string)=>path==='/profiles'?[{id:'fish',name:'大肥鱼',system_prompt:'甜甜的核心人设'},{id:'other',name:'其他人设',system_prompt:'另一份底色'}]:structuredClone(growth))})
afterEach(()=>vi.useRealTimers())
it('links to the centralized persona settings without changing learning',async()=>{
 const w=mount(Persona,{global:{stubs:{RouterLink:true}}});await flushPromises()
 expect(w.get('.persona-toggle').attributes('to')).toBe('/settings?tab=models')
 expect(put).not.toHaveBeenCalled();expect(w.text()).toContain('甜甜的核心人设');w.unmount()
})
it('a pending edit stays attached to its original persona after switching',async()=>{
 const w=mount(Persona,{global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.get('.persona-stories-heading button').trigger('click')
 const save=vi.mocked(openForm).mock.calls[0]![0].save
 await w.get('select').setValue('other');await flushPromises()
 await save({kind:'address',name:'小鱼',content:'喜欢被叫作小鱼。',locked:'true'})
 expect(post).toHaveBeenCalledWith('/persona/fish/traits',{kind:'address',name:'小鱼',content:'喜欢被叫作小鱼。',locked:true})
 expect(w.text()).toContain('另一份底色');w.unmount()
})

it('queues one manual reflection, polls progress, and stops when completed',async()=>{
 vi.useFakeTimers()
 let state={...growth,enabled:true,pending:22,job_status:null as string|null}
 vi.mocked(api).mockImplementation(async path=>path==='/profiles'?[{id:'fish',name:'小鱼',system_prompt:''}]:structuredClone(state))
 let finish:(v:any)=>void=()=>{}
 vi.mocked(post).mockImplementation(()=>new Promise(resolve=>{finish=resolve}))
 const w=mount(Persona,{global:{stubs:{RouterLink:true}}});await flushPromises()
 const button=w.get('.persona-reflect')
 await button.trigger('click');await button.trigger('click')
 expect(post).toHaveBeenCalledTimes(1)
 expect(post).toHaveBeenCalledWith('/persona/fish/reflect',{revision:2,request_id:'manual-test'})
 state.job_status='queued';finish({status:'queued'});await flushPromises()
 expect(button.attributes()).toHaveProperty('disabled');expect(w.text()).toContain('等待整理')
 state={...state,job_status:'running',pending:16};await vi.advanceTimersByTimeAsync(3000);await flushPromises()
 expect(w.text()).toContain('16 个新片段');expect(w.text()).toContain('正在分批整理')
 state={...state,job_status:null,pending:0};await vi.advanceTimersByTimeAsync(3000);await flushPromises()
 const calls=vi.mocked(api).mock.calls.length
 await vi.advanceTimersByTimeAsync(6000)
 expect(api).toHaveBeenCalledTimes(calls);expect(w.text()).toContain('0 个新片段')
 w.unmount()
})

it('disables manual reflection without permission and displays submission errors',async()=>{
 let state={...growth,enabled:true,pending:2,can_reflect:false}
 vi.mocked(api).mockImplementation(async path=>path==='/profiles'?[{id:'fish',name:'小鱼',system_prompt:''}]:structuredClone(state))
 const denied=mount(Persona,{global:{stubs:{RouterLink:true}}});await flushPromises()
 expect(denied.get('.persona-reflect').attributes()).toHaveProperty('disabled');denied.unmount()
 state.can_reflect=true
 vi.mocked(post).mockRejectedValueOnce(Error('养成设置已更新，请刷新后再试'))
 const w=mount(Persona,{global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.get('.persona-reflect').trigger('click');await flushPromises()
 expect(w.get('[role="alert"]').text()).toContain('养成设置已更新')
 expect(w.get('.persona-reflect').attributes()).not.toHaveProperty('disabled')
 w.unmount()
})
