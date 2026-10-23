import { mount, flushPromises } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import Settings from './Settings.vue'
import { openForm } from '../lib/ui'
import { put } from '../lib/api'

vi.mock('vue-router', () => ({useRoute:()=>({query:{tab:'models'}}), useRouter:()=>({replace:vi.fn()})}))
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),post:vi.fn(),patch:vi.fn(),remove:vi.fn(),requestId:()=> 'new-model',session:{user:{username:'test'}}}))
vi.mock('../lib/ui',async importOriginal=>({...await importOriginal<typeof import('../lib/ui')>(),openForm:vi.fn(),notify:vi.fn()}))
vi.mock('../lib/resource',async()=>{
 const {ref}=await import('vue')
 return {useResource:()=>({data:ref({profiles:[{id:'fish',name:'小鱼',protocol:'openai',base_url:'https://model.example/v1',model:'test',api_key:'********',context_window:1000000,max_output:8192,system_prompt:'基础人设'}],integrations:{},tokens:[]}),loading:ref(false),error:ref(''),load:vi.fn()})}
})

it('offers independent context/output presets and prevents impossible budgets before saving',async()=>{
 const w=mount(Settings,{global:{stubs:{GrowthSettings:true,RouterLink:true}}});await flushPromises()
 await w.get('[aria-label="编辑 小鱼"]').trigger('click')
 const form=vi.mocked(openForm).mock.calls[0]![0]
 const context=form.fields!.find(f=>f.key==='context_window')!, output=form.fields!.find(f=>f.key==='max_output')!
 expect(context.value).toBe(1000000);expect(output.value).toBe(8192)
 expect(context.presets).toContainEqual([128000,'128K']);expect(output.presets).toContainEqual([16384,'16K'])
 const values=Object.fromEntries(form.fields!.map(f=>[f.key,String(f.value ?? '')]))
 await expect(form.save({...values,context_window:'8192',max_output:'8192'})).rejects.toThrow('上下文')
 expect(put).not.toHaveBeenCalled()
 await form.save({...values,max_output:'16384'})
 expect(put).toHaveBeenCalledWith('/profiles',[expect.objectContaining({id:'fish',context_window:1000000,max_output:16384,api_key:'********',system_prompt:'基础人设'})])
 w.unmount()
})
