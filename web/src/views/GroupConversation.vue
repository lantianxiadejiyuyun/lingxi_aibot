<script setup lang="ts">
import MoodPanel from '../components/MoodPanel.vue'
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, MessageSquare, RefreshCw } from '@lucide/vue'
import { api, remove } from '../lib/api'
import { date, confirmAction, notify } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { QQGroup } from '../components/QQGroups.vue'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
import QQOutreachLog, {type Outreach} from '../components/QQOutreachLog.vue'
const route=useRoute(), group=computed(()=>String(route.params.group))
interface Message {id:string;qq_id:string;name:string;content:string;reply:string;occurred_at:number;delivery_status:string}
interface GroupResource {id:string;revision:number;value:{title?:string;content?:string;speaker?:string;quote?:string;url?:string;analysis?:{summary:string;thought:string}}}
interface Decision {event_id:string;action:string;reason:string;elapsed_ms:number;authorized:boolean}
const reasons:Record<string,string>={observe:'只观察 · 模型',observe_rule:'只观察 · 称呼规则',address_rule:'称呼规则',model:'模型判断',stale:'消息已过期',collection_command:'交给表情收集',check_limit:'判断额度已用完',reply_limit:'达到接话间隔或上限',cooldown:'刚回复过，安静一会',busy:'本地模型繁忙',unavailable:'模型不可用，保持安静',interrupted:'判断已中断'}
const actions:Record<string,string>={reply:'适合接话',wait:'等下一条',ignore:'安静旁听'}
const {data,loading,error,load}=useResource(async()=>{
  const [view,groups]=await Promise.all([api<{resources:Record<string,GroupResource[]>;conversation:{id:string};history:Message[];decisions:Decision[];outreach:Outreach[];reply_parts:Record<string,{position:number;body:string;status:string}[]>}>(`/qq/groups/${group.value}/conversation`),api<QQGroup[]>('/qq/groups')]);return {...view,group:groups.find(g=>g.group_id===group.value)}
},{resources:{} as Record<string,GroupResource[]>,conversation:{id:''},history:[] as Message[],decisions:[] as Decision[],outreach:[] as Outreach[],reply_parts:{} as Record<string,{position:number;body:string;status:string}[]>,group:undefined as QQGroup|undefined})
const decisions=computed(()=>Object.fromEntries((data.value.decisions||[]).map(d=>[d.event_id,d])))
const title=computed(()=>data.value.group?.settings.name||`QQ 群 ${group.value}`)
function forget(kind:string,item:GroupResource){const target=group.value;confirmAction('删除这条群资料？','只删除当前群这一条资料，不改变基础人设或其他会话。',async()=>{await remove(`/qq/groups/${target}/resources/${kind}/${encodeURIComponent(item.id)}?revision=${item.revision}`);await load();notify('群资料已删除')})}
const resourceNames:Record<string,string>={habit:'相处习惯',record:'共享记录',bilibili:'B 站心得'}
watch(group,load)
</script>
<template>
  <PageHeading :title="title" :description="`群号 ${group} · 一个群，一个独立会话`" eyebrow="ONE GROUP, ONE CONVERSATION"><RouterLink to="/qq" class="button secondary"><ArrowLeft :size="15" />返回群管理</RouterLink><button class="icon-button" aria-label="刷新群会话" @click="load"><RefreshCw :size="18" /></button></PageHeading>
  <MoodPanel :scope="`group:${group}`" />
  <ResourceState :loading="loading" :error="error" @retry="load"><section class="panel qq-conversation">
    <div class="section-heading"><div><h2><MessageSquare :size="18" />群聊记录</h2><p>只展示当前群的最近 100 条发言及回复。暂停或移除群不会清空历史。</p></div><span :class="['tag',data.group?.active?'green':'neutral']">{{data.group?.active?'接入中':'未接入'}}</span></div>
    <div v-if="data.group?.settings.system_prompt" class="qq-conversation-persona"><strong>这个群的交流风格</strong><p>{{data.group.settings.system_prompt}}</p></div>
    <EmptyState v-if="!data.history.length" title="这个群的故事，从这里开始" description="接入后，群里的发言与灵犀回复会汇集在这个独立会话中。" />
    <article v-for="m in data.history" :key="m.id" class="qq-message"><div class="section-heading"><strong>{{m.name}} <small>QQ {{m.qq_id}}</small></strong><time>{{date(m.occurred_at)}}</time></div><p>{{m.content}}</p><small v-if="decisions[m.id]" class="tag lilac">{{actions[decisions[m.id]!.action]}} · {{reasons[decisions[m.id]!.reason]||decisions[m.id]!.reason}} · {{(decisions[m.id]!.elapsed_ms/1000).toFixed(2)}} 秒</small>
      <template v-if="data.reply_parts?.[m.id]?.length && m.reply"><div v-for="p in data.reply_parts[m.id]" :key="p.position" class="qq-reply"><strong>灵犀 · 第 {{p.position+1}} 条 <small>{{p.status==='sent'?'已送达':p.status==='cancelled'?'已取消':p.status==='pending'?'等待发送':'发送未确认'}}</small></strong><p>{{p.body}}</p></div></template>
      <div v-else-if="m.reply" class="qq-reply"><strong>{{m.delivery_status==='sent'?'灵犀':'灵犀 · 发送未确认'}}</strong><p>{{m.reply}}</p></div>
    </article>
    <p v-if="data.conversation.id" class="qq-conversation-id">独立会话 {{data.conversation.id}}</p>
  </section><section class="panel" aria-label="群资料"><div class="section-heading"><div><h2>这个群积累的小记</h2><p>只属于本群。关闭功能后保留记录；可随时删除。</p></div><RouterLink to="/settings?tab=features" class="text-link">功能权限</RouterLink></div><div v-for="(label,kind) in resourceNames" :key="kind"><h3>{{label}}</h3><p v-if="!data.resources?.[kind]?.length" class="muted small">还没有记录。</p><article v-for="item in data.resources?.[kind]||[]" :key="item.id" class="qq-message"><div class="section-heading"><strong>{{item.value.title|| (item.value.speaker?`与 QQ ${item.value.speaker} 的相处`:'群资料')}}</strong><button class="text-link" @click="forget(kind,item)">删除</button></div><p>{{item.value.content||item.value.analysis?.summary}}</p><p v-if="item.value.analysis?.thought">{{item.value.analysis.thought}}</p><small v-if="item.value.quote" class="muted">依据：{{item.value.quote}}</small><a v-if="item.value.url" :href="item.value.url" target="_blank" rel="noopener noreferrer">查看视频来源</a></article></div></section><QQOutreachLog :items="data.outreach||[]" /></ResourceState>
</template>
