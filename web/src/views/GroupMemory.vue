<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { UsersRound, Settings2, Plus, ArrowUpRight, Database, ShieldCheck, Brain, Search, RefreshCw, Trash2, Pencil, Clock3, ArrowLeft, MessageSquare } from '@lucide/vue'
import { api, post, put, remove, requestId, session } from '../lib/api'
import { field, choice, openForm, notify, run, date, localDate, confirmAction } from '../lib/ui'
import { useResource } from '../lib/resource'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
import QQGroups from '../components/QQGroups.vue'
interface Person { group_id: string; qq_id: string; name: string; storage: string; memories?: number; messages: number; last_seen: number }
interface Fact { id: string; content: string; source: string; confidence?: number; expires_at?: number; evidence?: {event_id:string;quote:string}[]; revision?: number; indexed_revision?: number }
interface QQEvent { id:string; content:string; reply:string; occurred_at:number; delivery_status:string }
interface People { items: Person[]; owner_qq: string; enabled: boolean; pending_vectors: number; collection: string }
const selected=ref<Person|null>(null), detail=ref<{storage:string;memories:Fact[];history:QQEvent[]}>({storage:'qq',memories:[],history:[]}), detailLoading=ref(false), search=ref(''), detailError=ref('')
const route=useRoute(),groupFilter=ref('')
watch(()=>route.query.group,g=>{groupFilter.value=typeof g==='string'?g:''},{immediate:true})
let sequence=0
const { data,loading,error,load }=useResource(async()=>{
  return {people:await api<People>('/qq/people')}
},{people:{items:[],owner_qq:'',enabled:false,pending_vectors:0,collection:''} as People})
const visiblePeople=computed(()=>data.value.people.items.filter(p=>!groupFilter.value||p.group_id===groupFilter.value))
function filterGroup(group:string){groupFilter.value=group;sequence++;selected.value=null}
async function openPerson(person:Person,query='') {
  const current=++sequence;selected.value=person;detailLoading.value=true;detailError.value=''
  try {const result=await api(`/qq/person?group_id=${person.group_id}&qq_id=${person.qq_id}${query?'&q='+encodeURIComponent(query):''}`);if(current===sequence)detail.value=result}
  catch(e){if(current===sequence)detailError.value=e instanceof Error?e.message:'加载失败'}
  finally{if(current===sequence)detailLoading.value=false}
}
async function refresh(){await load();if(selected.value)await openPerson(selected.value)}
function remember(fact?:Fact){const person=selected.value;openForm({title:fact?'修正人物记忆':'记住一个人的特点',description:person?.storage==='main'?'主人记忆会保存到主库。':'记录有依据的偏好与沟通特点，避免绝对化标签。',fields:[{...field('group_id','群号',person?.group_id||'','text',true),readonly:!!person},{...field('qq_id','QQ 号',person?.qq_id||'','text',true),readonly:!!person},field('name','群昵称',person?.name||'','text',true),field('expires_at','有效期（可选）',localDate(fact?.expires_at),'datetime-local'),field('content','特点或偏好',fact?.content||'','textarea',true)],save:async v=>{
  const result=await post('/qq/memories',{group_id:v.group_id,qq_id:v.qq_id,name:v.name,content:v.content,expires_at:v.expires_at?Math.floor(new Date(v.expires_at).getTime()/1000):null});
  if(fact&&fact.content.trim()!==v.content.trim()&&fact.id!==result.id)await remove(person?.storage==='main'?`/memories/${fact.id}`:`/qq/memories/${fact.id}`)
  notify(result.storage==='main'?'主人记忆已存入主库':'记忆已保存，正在同步独立向量库');await refresh()
}})}
function forget(f:Fact){confirmAction('忘记这条人物记忆？','这条记录会立即停止参与检索；独立向量库中的副本在后台删除。',async()=>{await remove(selected.value?.storage==='main'?`/memories/${f.id}`:`/qq/memories/${f.id}`);await refresh()})}
function reindex(){confirmAction('重新同步人物向量？','使用当前嵌入模型重新生成索引，可能产生模型调用费用。',async()=>{const result=await post('/qq/reindex');notify(`已提交 ${result.queued} 条同步任务`);await load()})}
</script>
<template>
  <PageHeading title="群里的每个人，都有自己的样子" description="记住沟通习惯与偏好，也清楚每份记忆属于谁。" eyebrow="PEOPLE, UNDERSTOOD BETTER"><RouterLink class="button secondary" to="/settings?tab=qq"><Settings2 :size="16" />QQ 设置</RouterLink><button class="button" :disabled="!data.people.owner_qq" @click="remember()"><Plus :size="16" />添加人物记忆</button></PageHeading>
  <ResourceState :loading="loading" :error="error" @retry="load"><div class="qq-storage-map"><article><span class="stat-icon green"><ShieldCheck :size="23" /></span><div><h3>主人 · 主库</h3><p>{{ data.people.owner_qq || '先绑定主人 QQ 号' }}</p><small>性格、偏好与对话记录，留在你的个人空间。</small></div><RouterLink to="/memories" class="icon-button" aria-label="查看主库记忆"><ArrowUpRight :size="18" /></RouterLink></article><article><span class="stat-icon lilac"><Database :size="23" /></span><div><h3>群成员 · 独立向量库</h3><p>{{ data.people.pending_vectors ? `${data.people.pending_vectors} 条等待同步` : '群号 + QQ 号，分别存储' }}</p><small>群成员的特点不混入主人的长期记忆。</small></div><button class="icon-button" aria-label="重新同步向量" @click="reindex"><RefreshCw :size="17" /></button></article></div>
    <QQGroups :manage="false" @changed="load" @filter="filterGroup" />
    <div v-if="!selected"><div class="list-toolbar"><span class="section-label"><UsersRound :size="18" />{{groupFilter?`群 ${groupFilter} 的人物`:'人物记忆'}}<button v-if="groupFilter" class="text-link" @click="groupFilter=''">查看全部群</button></span><span :class="['tag',data.people.enabled?'green':'neutral']">{{ data.people.enabled?'群接入已启用':'群接入未启用' }}</span></div><EmptyState v-if="!visiblePeople.length" title="从一次相遇开始" description="绑定主人 QQ 和群白名单，连接 OneBot 与专用向量库。收到群消息后，这里会出现人物记录。"><RouterLink class="button secondary" to="/settings?tab=qq">设置群接入<ArrowUpRight :size="15" /></RouterLink></EmptyState><div v-else class="people-grid"><button v-for="p in visiblePeople" :key="p.group_id+':'+p.qq_id" class="person-card" @click="search='';openPerson(p)"><div class="section-heading"><span class="person-avatar">{{ p.name.slice(0,1) }}</span><span :class="['tag',p.storage==='main'?'green':'lilac']">{{ p.storage==='main'?'主人 · 主库':'群成员 · 独立库' }}</span></div><h3>{{ p.name }}</h3><p>QQ {{ p.qq_id }}<br />群 {{ p.group_id }}</p><div class="person-footer"><span>{{ p.storage==='main'?'个人长期记忆':`${p.memories||0} 条人物记忆` }}</span><ArrowUpRight :size="15" /></div></button></div></div>
    <section v-else class="person-detail"><div class="section-heading"><div class="inline-actions"><button class="icon-button" aria-label="返回人物列表" @click="sequence++;selected=null"><ArrowLeft :size="18" /></button><div><h2>{{ selected.name }}</h2><p>QQ {{ selected.qq_id }} · 群 {{ selected.group_id }}</p></div></div><span :class="['tag',selected.storage==='main'?'green':'lilac']">{{ selected.storage==='main'?'存储在主库':'独立群成员向量库' }}</span></div><form v-if="selected.storage!=='main'" class="large-search" @submit.prevent="openPerson(selected,search)"><Search :size="19" /><input v-model="search" placeholder="按语义查找这个人的特点…" aria-label="搜索人物记忆" /><button class="button" :disabled="detailLoading">检索</button></form><ResourceState :loading="detailLoading" :error="detailError" @retry="openPerson(selected)"><div class="qq-detail-columns"><section class="panel"><div class="section-heading"><h2><Brain :size="18" />{{ selected.storage==='main'?'主人的长期记忆':'人物特点' }}</h2><button class="text-link" @click="remember()"><Plus :size="14" />补充</button></div><EmptyState v-if="!detail.memories.length" title="了解，需要一点时间" description="稳定的性格观察需要多条本人发言支持。" /><article v-for="f in detail.memories" :key="f.id" class="person-fact"><p>{{ f.content }}</p><div class="section-heading"><span class="tag neutral">{{ f.source==='manual'?'手动记录':`对话观察${f.confidence?' · '+Math.round(f.confidence*100)+'%':''}` }}</span><div class="inline-actions"><button class="icon-button" aria-label="修正这条记忆" @click="remember(f)"><Pencil :size="14" /></button><button class="icon-button danger" aria-label="忘记这条记忆" @click="forget(f)"><Trash2 :size="14" /></button></div></div><details v-if="f.evidence?.length" class="evidence"><summary>查看依据</summary><blockquote v-for="(e,i) in f.evidence" :key="i">{{ e.quote }}<small>{{ e.event_id.slice(0,12) }}</small></blockquote></details><small v-if="f.expires_at" class="muted"><Clock3 :size="11" /> 有效至 {{ date(f.expires_at) }}</small><small v-if="f.revision && f.revision!==f.indexed_revision" class="index-pending">向量同步中</small></article></section><section class="panel"><div class="section-heading"><h2><MessageSquare :size="18" />最近对话</h2><span class="muted small">最近 50 条</span></div><EmptyState v-if="!detail.history.length" title="还没有收到发言" description="人物特点和对话记录会按身份分别保存。" /><article v-for="e in detail.history" :key="e.id" class="qq-message"><small>{{ date(e.occurred_at) }}</small><p>{{ e.content }}</p><div v-if="e.reply" class="qq-reply"><strong>{{ e.delivery_status==='sent'?'灵犀回复':'回复（发送未确认）' }}</strong><p>{{ e.reply }}</p></div></article></section></div></ResourceState></section>
    <details class="qq-connection"><summary>存储信息</summary><p>群成员原始消息：qq/people.sqlite3 · 专用 Qdrant 集合：{{ data.people.collection }}</p></details>
  </ResourceState>
</template>
