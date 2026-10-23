import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import QQGroups from './QQGroups.vue'
import { api, post, put } from '../lib/api'
import { openForm } from '../lib/ui'
vi.mock('../lib/api',()=>({api:vi.fn(),post:vi.fn(),put:vi.fn(),remove:vi.fn()}))
vi.mock('../lib/ui',()=>({choice:vi.fn(),field:vi.fn(),openForm:vi.fn(),confirmAction:vi.fn(),notify:vi.fn(),date:()=>''}))
const groups=[{group_id:'30001',settings:{name:'摄影群',enabled:true,profile_id:null,system_prompt:'聊摄影',reply_mentioned:null,collect_memory:null},conversation:{id:'session-a'},messages:5,last_seen:null,active:true,profile_id:null,reply_mentioned:true,collect_memory:true},{group_id:'30002',settings:{name:'游戏群',enabled:false,profile_id:null,system_prompt:'聊游戏',reply_mentioned:false,collect_memory:false},conversation:{id:'session-b'},messages:2,last_seen:null,active:false,profile_id:null,reply_mentioned:false,collect_memory:false}]
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async path=>path==='/qq/groups'?groups:[])})
function setup(){return mount(QQGroups,{global:{stubs:{RouterLink:{props:['to'],template:'<a :href="to"><slot /></a>'}}}})}
it('shows a separate conversation link and controls for each group',async()=>{
 const w=setup();await flushPromises()
 expect(w.findAll('.qq-group-card')).toHaveLength(2)
 expect(w.findAll('.qq-group-actions a').map(a=>a.attributes('href'))).toEqual(['/qq/groups/30001','/qq/groups/30002'])
 await w.find('[aria-label="查看群 30002 的人物"]').trigger('click')
 expect(w.emitted('filter')).toEqual([['30002']]);expect(post).not.toHaveBeenCalled();w.unmount()
})
it('saves one group settings without replacing the shared integration config',async()=>{
 const w=setup();await flushPromises();await w.find('[aria-label="设置群 30002"]').trigger('click')
 const form=vi.mocked(openForm).mock.calls[0]![0]
 await form.save({group_id:'30002',name:'游戏群',enabled:'false',profile_id:'special',system_prompt:'轻松聊天',reply_mentioned:'true',collect_memory:'inherit'})
 expect(put).toHaveBeenCalledWith('/qq/groups/30002',{name:'游戏群',enabled:false,profile_id:'special',system_prompt:'轻松聊天',reply_mentioned:true,collect_memory:null,decision:{mode:'off',cooldown_secs:120,max_replies_per_hour:6}})
 expect(post).not.toHaveBeenCalled();w.unmount()
})
it('adds a group through its own endpoint and retains failures in the form',async()=>{
 const w=setup();await flushPromises();await w.find('.qq-groups>.section-heading button').trigger('click')
 const form=vi.mocked(openForm).mock.calls[0]![0]
 vi.mocked(post).mockRejectedValueOnce(new Error('这个群已添加'))
 await expect(form.save({group_id:'30003',name:'新群',enabled:'true',profile_id:'',system_prompt:'',reply_mentioned:'inherit',collect_memory:'false'})).rejects.toThrow('这个群已添加')
 expect(post).toHaveBeenCalledWith('/qq/groups',{group_id:'30003',settings:expect.objectContaining({name:'新群',profile_id:null,collect_memory:false})})
 expect(w.emitted('changed')).toBeUndefined();w.unmount()
})
it('saves dialogue switches for one group while retaining its persona and reply policy',async()=>{
 const w=setup();await flushPromises();await w.find('[aria-label="聊天节奏 30001"]').trigger('click')
 const form=vi.mocked(openForm).mock.calls[0]![0]
 await form.save({split:'true',max_messages:'4',interval_secs:'2',followup:'true',delay_minutes:'3',max_followups:'5',followup_daily:'8',proactive:'false',topics:'摄影\n电影'})
 expect(put).toHaveBeenCalledWith('/qq/groups/30001',expect.objectContaining({system_prompt:'聊摄影',reply_mentioned:null,reply_style:{enabled:true,max_messages:4,interval_secs:2},followup:{enabled:true,delay_minutes:3,max_followups:5,daily_limit:8},proactive:expect.objectContaining({enabled:false,topics:['摄影','电影']})}))
 expect(put).toHaveBeenCalledTimes(1);w.unmount()
})
