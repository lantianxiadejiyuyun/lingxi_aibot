import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import FeaturePermissions from './FeaturePermissions.vue'
import { api, put } from '../lib/api'

vi.mock('../lib/api',()=>({api:vi.fn(),put:vi.fn()}))
vi.mock('../lib/ui',()=>({notify:vi.fn()}))
const initial=()=>({enabled:true,features:Object.fromEntries(['chat','vision','sticker_collection','sticker_reply','memory','mood','persona_growth','proactive','bilibili','web_search','tools'].map(key=>[key,{enabled:true,private:true,group:!['persona_growth','bilibili','web_search','tools'].includes(key)}]))})
beforeEach(()=>{vi.clearAllMocks();vi.mocked(api).mockImplementation(async()=>initial());vi.mocked(put).mockImplementation(async(_path,body)=>body as any)})
it('changes only group permission and preserves the private grant',async()=>{
 const w=mount(FeaturePermissions);await flushPromises()
 await w.get('input[aria-label="图片与 GIF 识别 · QQ 群聊"]').setValue(false);await flushPromises()
 expect(put).toHaveBeenCalledWith('/permissions',expect.objectContaining({features:expect.objectContaining({vision:{enabled:true,private:true,group:false}})}))
 expect(w.findAll('input[aria-label="人设养成 · QQ 群聊"]')).toHaveLength(1)
 w.unmount()
})
it('master switch pauses controls without erasing individual choices',async()=>{
 const w=mount(FeaturePermissions);await flushPromises()
 await w.get('input[aria-label="AI 功能总开关"]').setValue(false);await flushPromises()
 expect(vi.mocked(put).mock.calls[0]![1]).toEqual({...initial(),enabled:false})
 const privateGrant=w.get('input[aria-label="对话与回复 · 个人会话"]').element as HTMLInputElement
 expect(privateGrant.disabled).toBe(true);expect(privateGrant.checked).toBe(true)
 await w.get('input[aria-label="AI 功能总开关"]').setValue(true);await flushPromises()
 expect(privateGrant.disabled).toBe(false);w.unmount()
})
it('restores the displayed switch when saving fails',async()=>{
 const w=mount(FeaturePermissions);await flushPromises()
 vi.mocked(put).mockRejectedValueOnce(new Error('连接失败'))
 await w.get('input[aria-label="AI 功能总开关"]').setValue(false);await flushPromises()
 expect((w.get('input[aria-label="AI 功能总开关"]').element as HTMLInputElement).checked).toBe(true)
 expect(w.get('[role="alert"]').text()).toBe('连接失败');w.unmount()
})

it('exposes every group switch and saves web search independently of tools',async()=>{
 const w=mount(FeaturePermissions);await flushPromises()
 expect(w.findAll('input[aria-label$="QQ 群聊"]')).toHaveLength(11)
 await w.get('input[aria-label="联网搜索 · 个人会话"]').setValue(false);await flushPromises()
 const value=vi.mocked(put).mock.calls[0]![1] as ReturnType<typeof initial>
 expect(value.features.web_search!.private).toBe(false)
 expect(value.features.tools!.private).toBe(true)
 await w.get('input[aria-label="AI 工具操作 · QQ 群聊"]').setValue(true);await flushPromises()
 expect((vi.mocked(put).mock.calls[1]![1] as ReturnType<typeof initial>).features.tools!.group).toBe(true)
 w.unmount()
})
