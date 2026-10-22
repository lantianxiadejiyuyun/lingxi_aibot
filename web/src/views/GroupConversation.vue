<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, MessageSquare, RefreshCw } from '@lucide/vue'
import { api } from '../lib/api'
import { date } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { QQGroup } from '../components/QQGroups.vue'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const route=useRoute(), group=computed(()=>String(route.params.group))
interface Message {id:string;qq_id:string;name:string;content:string;reply:string;occurred_at:number;delivery_status:string}
const {data,loading,error,load}=useResource(async()=>{
  const [view,groups]=await Promise.all([api<{conversation:{id:string};history:Message[]}>(`/qq/groups/${group.value}/conversation`),api<QQGroup[]>('/qq/groups')]);return {...view,group:groups.find(g=>g.group_id===group.value)}
},{conversation:{id:''},history:[] as Message[],group:undefined as QQGroup|undefined})
const title=computed(()=>data.value.group?.settings.name||`QQ 群 ${group.value}`)
watch(group,load)
</script>
<template>
  <PageHeading :title="title" :description="`群号 ${group} · 一个群，一个独立会话`" eyebrow="ONE GROUP, ONE CONVERSATION"><RouterLink to="/qq" class="button secondary"><ArrowLeft :size="15" />返回群管理</RouterLink><button class="icon-button" aria-label="刷新群会话" @click="load"><RefreshCw :size="18" /></button></PageHeading>
  <ResourceState :loading="loading" :error="error" @retry="load"><section class="panel qq-conversation">
    <div class="section-heading"><div><h2><MessageSquare :size="18" />群聊记录</h2><p>只展示当前群的最近 100 条发言及回复。暂停或移除群不会清空历史。</p></div><span :class="['tag',data.group?.active?'green':'neutral']">{{data.group?.active?'接入中':'未接入'}}</span></div>
    <div v-if="data.group?.settings.system_prompt" class="qq-conversation-persona"><strong>这个群的交流风格</strong><p>{{data.group.settings.system_prompt}}</p></div>
    <EmptyState v-if="!data.history.length" title="这个群的故事，从这里开始" description="接入后，群里的发言与灵犀回复会汇集在这个独立会话中。" />
    <article v-for="m in data.history" :key="m.id" class="qq-message"><div class="section-heading"><strong>{{m.name}} <small>QQ {{m.qq_id}}</small></strong><time>{{date(m.occurred_at)}}</time></div><p>{{m.content}}</p><div v-if="m.reply" class="qq-reply"><strong>{{m.delivery_status==='sent'?'灵犀':'灵犀 · 发送未确认'}}</strong><p>{{m.reply}}</p></div></article>
    <p v-if="data.conversation.id" class="qq-conversation-id">独立会话 {{data.conversation.id}}</p>
  </section></ResourceState>
</template>
