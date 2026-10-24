import {mount,flushPromises} from '@vue/test-utils'
import {beforeEach,expect,it,vi} from 'vitest'
import {createRouter,createMemoryHistory} from 'vue-router'
import QQSettings from './QQSettings.vue'
import {api,put} from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),requestId:()=> 'test-secret',session:{user:{id:'owner',is_admin:true}}}))
vi.mock('../lib/ui',()=>({notify:vi.fn()}))
beforeEach(()=>vi.clearAllMocks())
it('saves shared QQ settings without overwriting freshly changed group and channel settings',async()=>{
 const original={qq:{enabled:true,private_enabled:true,owner_qq:'10001',bot_qq:'20001',groups:['30001'],onebot:{url:'http://napcat:3000',token:'••••'},inbound:{secret:'••••'},retention_days:30}}
 let fresh:any=original
 vi.mocked(api).mockImplementation(async path=>path==='/profiles'?[{id:'memory-preset',name:'群人物整理'}]:fresh)
 vi.mocked(put).mockResolvedValue({})
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{template:'<div />'}}]})
 const w=mount(QQSettings,{global:{plugins:[router],stubs:{NapCatConnect:true,QQGroups:true,QQDecision:true,QQPrivateRhythm:true}}});await flushPromises()
 const mention=w.findAll('label').find(label=>label.text().includes('群回复时 @发送者'))!.get('input')
 expect((mention.element as HTMLInputElement).checked).toBe(false)
 await mention.setValue(true)
 const memory=w.findAll('label').find(label=>label.text().includes('群人物特点生成模型'))!.get('select')
 expect(memory.findAll('option').map(option=>[option.attributes('value'),option.text()])).toEqual([['','沿用各群对话模型'],['memory-preset','群人物整理']])
 await memory.setValue('memory-preset')
 await w.get('input[type="number"]').setValue(90)
 fresh={...original,feishu:{app_id:'current'},qq:{...original.qq,groups:['30001','30002'],group_settings:{'30002':{name:'新增群'}},followup:{enabled:true}}}
 await w.get('form').trigger('submit');await flushPromises()
 expect(put).toHaveBeenCalledWith('/integrations',expect.objectContaining({feishu:fresh.feishu,qq:expect.objectContaining({groups:fresh.qq.groups,group_settings:fresh.qq.group_settings,followup:{enabled:true},memory_profile_id:'memory-preset',retention_days:90,reply_mention_sender:true,onebot:original.qq.onebot})}))
 w.unmount()
})
it('loads a saved group memory preset and can return to each group conversation model',async()=>{
 const original={qq:{enabled:true,owner_qq:'10001',bot_qq:'20001',memory_profile_id:'saved',onebot:{url:'http://napcat:3000',token:'••••'},inbound:{secret:'••••'}}}
 vi.mocked(api).mockImplementation(async path=>path==='/profiles'?[{id:'saved',name:'已有模型'}]:original)
 vi.mocked(put).mockResolvedValue({})
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{template:'<div />'}}]})
 const w=mount(QQSettings,{global:{plugins:[router],stubs:{NapCatConnect:true,QQGroups:true,QQDecision:true,QQPrivateRhythm:true}}});await flushPromises()
 const memory=w.findAll('label').find(label=>label.text().includes('群人物特点生成模型'))!.get('select')
 expect((memory.element as HTMLSelectElement).value).toBe('saved')
 await memory.setValue('')
 await w.get('form').trigger('submit');await flushPromises()
 expect(put).toHaveBeenCalledWith('/integrations',expect.objectContaining({qq:expect.objectContaining({memory_profile_id:null,onebot:original.qq.onebot,inbound:original.qq.inbound})}))
 w.unmount()
})
