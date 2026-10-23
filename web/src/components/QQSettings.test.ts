import {mount,flushPromises} from '@vue/test-utils'
import {expect,it,vi} from 'vitest'
import {createRouter,createMemoryHistory} from 'vue-router'
import QQSettings from './QQSettings.vue'
import {api,put} from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn(),requestId:()=> 'test-secret',session:{user:{id:'owner',is_admin:true}}}))
vi.mock('../lib/ui',()=>({notify:vi.fn()}))
it('saves shared QQ settings without overwriting freshly changed group and channel settings',async()=>{
 const original={qq:{enabled:true,private_enabled:true,owner_qq:'10001',bot_qq:'20001',groups:['30001'],onebot:{url:'http://napcat:3000',token:'••••'},inbound:{secret:'••••'},retention_days:30}}
 let fresh:any=original
 vi.mocked(api).mockImplementation(async path=>path==='/profiles'?[]:fresh)
 vi.mocked(put).mockResolvedValue({})
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{template:'<div />'}}]})
 const w=mount(QQSettings,{global:{plugins:[router],stubs:{NapCatConnect:true,QQGroups:true,QQDecision:true,QQPrivateRhythm:true}}});await flushPromises()
 const mention=w.findAll('label').find(label=>label.text().includes('群回复时 @发送者'))!.get('input')
 expect((mention.element as HTMLInputElement).checked).toBe(false)
 await mention.setValue(true)
 await w.get('input[type="number"]').setValue(90)
 fresh={...original,feishu:{app_id:'current'},qq:{...original.qq,groups:['30001','30002'],group_settings:{'30002':{name:'新增群'}},followup:{enabled:true}}}
 await w.get('form').trigger('submit');await flushPromises()
 expect(put).toHaveBeenCalledWith('/integrations',expect.objectContaining({feishu:fresh.feishu,qq:expect.objectContaining({groups:fresh.qq.groups,group_settings:fresh.qq.group_settings,followup:{enabled:true},retention_days:90,reply_mention_sender:true,onebot:original.qq.onebot})}))
 w.unmount()
})
