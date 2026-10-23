import { mount,flushPromises } from '@vue/test-utils'
import { expect,it,vi } from 'vitest'
import GrowthSettings from './GrowthSettings.vue'
import {api,put} from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn()}))
vi.mock('../lib/ui',()=>({notify:vi.fn()}))
it('saves the selected persona with its revision',async()=>{
 vi.mocked(api).mockResolvedValue({enabled:false,revision:2})
 vi.mocked(put).mockResolvedValue({enabled:true,revision:3})
 const w=mount(GrowthSettings,{props:{profiles:[{id:'fish',name:'小鱼'} as any,{id:'other',name:'其他'} as any]},global:{stubs:{RouterLink:true}}});await flushPromises()
 await w.get('select').setValue('other');await flushPromises()
 await w.get('input').setValue(true);await flushPromises()
 expect(put).toHaveBeenCalledWith('/persona/other',{enabled:true,revision:2});w.unmount()
})
