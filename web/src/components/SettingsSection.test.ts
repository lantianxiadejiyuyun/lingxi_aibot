import { mount } from '@vue/test-utils'
import { expect,it } from 'vitest'
import { defineComponent, ref } from 'vue'
import SettingsSection from './SettingsSection.vue'
it('loads details only when opened and keeps drafts on subsequent folding',async()=>{
 const Draft=defineComponent({setup:()=>({value:ref('原值')}),template:'<input v-model="value">'})
 const w=mount(SettingsSection,{props:{title:'更多',description:'细节'},slots:{default:Draft}})
 expect(w.find('input').exists()).toBe(false)
 await w.setProps({initialOpen:true});expect(w.find('input').exists()).toBe(true)
 await w.find('input').setValue('未保存的编辑')
 await w.setProps({initialOpen:false});expect((w.find('input').element as HTMLInputElement).value).toBe('未保存的编辑')
 w.unmount()
})
