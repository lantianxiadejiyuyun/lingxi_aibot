<script setup lang="ts">
import { Plus, Settings2, MessageSquare, Trash2, ArrowUpRight, UsersRound } from '@lucide/vue'
import { api, post, put, remove } from '../lib/api'
import { choice, field, openForm, confirmAction, notify, date } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import ResourceState from './ResourceState.vue'
import EmptyState from './EmptyState.vue'

export interface GroupSettings { name:string; enabled:boolean; profile_id:string|null; system_prompt:string; reply_mentioned:boolean|null; collect_memory:boolean|null }
export interface QQGroup { group_id:string; settings:GroupSettings; conversation:{id:string;updated_at:number}; messages:number; last_seen:number|null; active:boolean; profile_id:string|null; reply_mentioned:boolean; collect_memory:boolean }
const emit = defineEmits<{changed:[];filter:[group:string]}>()
const {data,loading,error,load}=useResource(async()=>{
  const [groups,profiles]=await Promise.all([api<QQGroup[]>('/qq/groups'),api<Profile[]>('/profiles')])
  return {groups,profiles}
},{groups:[] as QQGroup[],profiles:[] as Profile[]})
const inherit=(value:boolean|null|undefined)=>value==null?'inherit':String(value)
const toggle=(value:string)=>value==='inherit'?null:value==='true'
function edit(group?:QQGroup) {
  const c=group?.settings
  openForm({title:group?'群会话设置':'添加 QQ 群',description:'一个群对应一个独立会话。群内上下文与回复不会串到其他群。',fields:[
    {...field('group_id','群号',group?.group_id||'','text',true),readonly:!!group},
    field('name','群名称',c?.name||''),
    choice('enabled','这个群的接入',[['true','启用'],['false','暂停']],c?.enabled??true),
    choice('profile_id','负责这个群的模型',[['','沿用接入默认模型'],...data.value.profiles.map(p=>[p.id,p.name] as [string,string])],c?.profile_id||''),
    {...field('system_prompt','这个群的人设与交流风格',c?.system_prompt||'','textarea'),hint:'仅用于当前群，最多2000字。留空使用默认群聊风格，不加载私人助手的人设与记忆。'},
    choice('reply_mentioned','被 @ 时回复',[['inherit','沿用接入设置'],['true','开启'],['false','关闭']],inherit(c?.reply_mentioned)),
    choice('collect_memory','提取人物特点',[['inherit','沿用接入设置'],['true','开启'],['false','关闭']],inherit(c?.collect_memory)),
  ],save:async v=>{
    const settings:GroupSettings={name:v.name.trim(),enabled:v.enabled==='true',profile_id:v.profile_id||null,system_prompt:v.system_prompt.trim(),reply_mentioned:toggle(v.reply_mentioned),collect_memory:toggle(v.collect_memory)}
    if(group) await put(`/qq/groups/${group.group_id}`,settings)
    else await post('/qq/groups',{group_id:v.group_id.trim(),settings})
    notify(group?'群会话设置已保存':'群已添加，独立会话已建立')
    await load();emit('changed')
  }})
}
function disconnect(group:QQGroup) {
  confirmAction(`移除 ${group.settings.name||group.group_id}？`,'停止接收和回复这个群。已有对话与人物记忆保留；重新添加同一群号时沿用原会话。',async()=>{
    await remove(`/qq/groups/${group.group_id}`);await load();emit('changed');emit('filter','');notify('群已移除，历史记录保留')
  })
}
function modelName(group:QQGroup) {return data.value.profiles.find(p=>p.id===group.profile_id)?.name||'接入默认模型'}
defineExpose({load})
</script>
<template>
  <section class="qq-groups">
    <div class="section-heading"><div><h2>群与独立会话</h2><p>每个群都有自己的上下文、模型与交流方式。</p></div><button class="button" @click="edit()"><Plus :size="16" />添加群</button></div>
    <ResourceState :loading="loading" :error="error" @retry="load">
      <EmptyState v-if="!data.groups.length" title="为每个群，留一个会话" description="先添加群号，再配置 NapCat 接入。可以添加多个群，分别管理。"><button class="button secondary" @click="edit()">添加第一个群</button></EmptyState>
      <div v-else class="qq-groups-grid"><article v-for="g in data.groups" :key="g.group_id" class="qq-group-card">
        <div class="section-heading"><span class="stat-icon green"><MessageSquare :size="21" /></span><span :class="['tag',g.active?'green':'neutral']">{{g.active?'接入中':g.settings.enabled?'等待开启接入':'已暂停'}}</span></div>
        <h3>{{g.settings.name||`QQ 群 ${g.group_id}`}}</h3><p>群号 {{g.group_id}}</p>
        <div class="qq-group-meta"><span>{{modelName(g)}}</span><span>{{g.reply_mentioned?'@ 时回复':'仅记录'}}</span></div>
        <p class="qq-group-persona">{{g.settings.system_prompt||'默认群聊风格，只参考这个群里的交流。'}}</p>
        <div class="qq-group-activity"><span>{{g.messages}} 条发言</span><span>{{g.last_seen?date(g.last_seen):'等待第一条消息'}}</span></div>
        <div class="qq-group-actions"><RouterLink :to="`/qq/groups/${g.group_id}`" class="text-link">打开会话<ArrowUpRight :size="14" /></RouterLink><button class="icon-button" :aria-label="`查看群 ${g.group_id} 的人物`" @click="emit('filter',g.group_id)"><UsersRound :size="15" /></button><button class="icon-button" :aria-label="`设置群 ${g.group_id}`" @click="edit(g)"><Settings2 :size="15" /></button><button class="icon-button danger" :aria-label="`移除群 ${g.group_id}`" @click="disconnect(g)"><Trash2 :size="15" /></button></div>
      </article></div>
    </ResourceState>
  </section>
</template>
