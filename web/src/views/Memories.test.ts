import { mount,flushPromises } from '@vue/test-utils'
import { beforeEach,expect,it,vi } from 'vitest'
import Memories from './Memories.vue'
import { api,patch,put } from '../lib/api'
vi.mock('../lib/api',()=>({api:vi.fn(),patch:vi.fn(),put:vi.fn(),post:vi.fn(),remove:vi.fn(),requestId:()=> 'test'}))
vi.mock('../lib/ui',()=>({notify:vi.fn(),date:()=> '今天',run:async(f:()=>Promise<unknown>)=>f(),choice:vi.fn(),field:vi.fn(),openForm:vi.fn(),confirmAction:vi.fn()}))
const data={config:{enabled:true,auto_extract:true,half_life_days:30,max_recall:6,relevance_threshold:.18,daily_auto_limit:8},entries:[{id:'old-trip',content:'去年在西湖摄影',importance:3,memory_type:'episode',semantic_key:'',source:'auto',status:'active',freshness:.4,recall_count:2,created_at:123}],automatic_today:1,last_recall:null}
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async path=>path.startsWith('/memory/preview')?[{id:'old-trip',content:'去年在西湖摄影',relevance:.9,freshness:.4,selected:false,reason:'话题相符'}]:data)})
it('revives and pins a memory through the same owned record',async()=>{
 const w=mount(Memories);await flushPromises();await w.findAll('button').find(b=>b.text()==='重新唤起')!.trigger('click');await flushPromises()
 expect(patch).toHaveBeenCalledWith('/memories/old-trip',{reinforce:true});
 await w.findAll('button').find(b=>b.text()==='设为长期事实')!.trigger('click');await flushPromises();expect(patch).toHaveBeenCalledWith('/memories/old-trip',{memory_type:'fact'});w.unmount()
})
it('preview is read-only and says it is not an actual recall',async()=>{
 const w=mount(Memories);await flushPromises();await w.get('[aria-label="试着检索记忆"]').setValue('西湖摄影');await w.findAll('form')[1]!.trigger('submit');await flushPromises()
 expect(api).toHaveBeenCalledWith('/memory/preview?q='+encodeURIComponent('西湖摄影'));expect(patch).not.toHaveBeenCalled();expect(put).not.toHaveBeenCalled();expect(w.text()).toContain('当前显示预览结果');w.unmount()
})
