import { mount,flushPromises } from '@vue/test-utils'
import { expect,it,vi,beforeEach } from 'vitest'
import Persona from './Persona.vue'
import {api,put,post} from '../lib/api'
import {openForm} from '../lib/ui'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),post:vi.fn(),remove:vi.fn()}))
vi.mock('../lib/ui',()=>({choice:vi.fn(),field:vi.fn(),openForm:vi.fn(),confirmAction:vi.fn(),date:()=>'',notify:vi.fn(),run:(fn:()=>Promise<void>)=>fn()}))
const growth={enabled:false,revision:2,turns:0,days:0,pending:0,last_reflected_at:null,last_error:'',traits:[]}
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async(path:string)=>path==='/profiles'?[{id:'fish',name:'大肥鱼',system_prompt:'甜甜的核心人设'},{id:'other',name:'其他人设',system_prompt:'另一份底色'}]:growth)})
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
