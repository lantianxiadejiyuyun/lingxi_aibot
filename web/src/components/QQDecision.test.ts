import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import QQDecision from './QQDecision.vue'
import { api, post, put } from '../lib/api'
import { openForm } from '../lib/ui'
vi.mock('../lib/api',()=>({api:vi.fn(),post:vi.fn(),put:vi.fn()}))
vi.mock('../lib/ui',()=>({field:vi.fn(),openForm:vi.fn(),notify:vi.fn(),run:(action:()=>unknown)=>action()}))
beforeEach(()=>vi.clearAllMocks())
it('probes without sending and displays measured latency',async()=>{
  vi.mocked(post).mockResolvedValue({action:'wait',elapsed_ms:1234,model:'qwen3:0.6b'})
  const w=mount(QQDecision,{props:{settings:{url:'http://ollama:11434',model:'qwen3:0.6b'}}})
  await w.find('input').setValue('大肥鱼等我一下')
  await w.find('form').trigger('submit');await flushPromises()
  expect(post).toHaveBeenCalledWith('/qq/decision/probe',{text:'大肥鱼等我一下'})
  expect(w.get('[role=status]').text()).toContain('等下一条 · 1.23 秒')
  expect(put).not.toHaveBeenCalled();w.unmount()
})
it('preserves existing channel and group settings when configuring the classifier',async()=>{
  vi.mocked(api).mockResolvedValue({search:{url:'http://search'},qq:{groups:['30001'],group_settings:{'30001':{name:'摄影群'}},onebot:{token:'********'}}})
  const w=mount(QQDecision)
  await w.find('button').trigger('click');await flushPromises()
  await vi.mocked(openForm).mock.calls[0]![0].save({url:'http://ollama:11434',model:'qwen3:0.6b',aliases:'大肥鱼，鱼鱼',timeout_secs:'30',max_checks_per_hour:'60'})
  expect(put).toHaveBeenCalledWith('/integrations',expect.objectContaining({search:{url:'http://search'},qq:expect.objectContaining({groups:['30001'],group_settings:{'30001':{name:'摄影群'}},decision:{url:'http://ollama:11434',model:'qwen3:0.6b',aliases:['大肥鱼','鱼鱼'],timeout_secs:30,max_checks_per_hour:60}})}))
  expect(w.emitted('changed')).toHaveLength(1);w.unmount()
})
