<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Settings2 } from '@lucide/vue'
import { api, put, requestId, session } from '../lib/api'
import { notify } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import ResourceState from './ResourceState.vue'
import NapCatConnect from './NapCatConnect.vue'
import QQGroups from './QQGroups.vue'
import QQDecision from './QQDecision.vue'
import QQPrivateRhythm from './QQPrivateRhythm.vue'
const origin=window.location.origin
const router=useRouter(),draft=ref<Record<string,any>>({}),saving=ref(false),saveError=ref('')
const {data,loading,error,load}=useResource(async()=>{const [integrations,profiles]=await Promise.all([api<Record<string,any>>('/integrations'),api<Profile[]>('/profiles')]);return {integrations,profiles}},{integrations:{} as Record<string,any>,profiles:[] as Profile[]})
watch(data,v=>{const q=v.integrations.qq||{};draft.value={...q,owner_qq:q.owner_qq||'',bot_qq:q.bot_qq||'',private_enabled:!!q.private_enabled,enabled:!!q.enabled,profile_id:q.profile_id||'',retention_days:q.retention_days||30,collect_memory:q.collect_memory!==false,reply_mentioned:q.reply_mentioned!==false,onebot:{url:'',token:'',...q.onebot},inbound:{secret:requestId()+requestId(),...q.inbound},vector:{url:'',api_key:'',...q.vector},embedding:{url:'',api_key:'',model:'',...q.embedding}}})
async function save(){saving.value=true;saveError.value='';try{const fresh=await api<Record<string,any>>('/integrations'),d=draft.value;await put('/integrations',{...fresh,qq:{...fresh.qq,enabled:d.enabled,private_enabled:d.private_enabled,owner_qq:d.owner_qq.trim(),bot_qq:d.bot_qq.trim(),profile_id:d.profile_id||null,retention_days:Number(d.retention_days),collect_memory:d.collect_memory,reply_mentioned:d.reply_mentioned,onebot:{...fresh.qq?.onebot,...d.onebot},inbound:d.inbound,vector:{...fresh.qq?.vector,...d.vector},embedding:{...fresh.qq?.embedding,...d.embedding}}});await load();notify('QQ 连接与默认设置已保存')}catch(e){saveError.value=e instanceof Error?e.message:'保存失败'}finally{saving.value=false}}
</script>
<template>
 <ResourceState :loading="loading" :error="error" @retry="load">
  <NapCatConnect @configured="load" /><p v-if="!session.user?.is_admin" class="muted small">外部 NapCat 的 HTTP 客户端回调：<code>{{origin}}/api/v2/qq/inbound/{{session.user?.id}}</code>；Token 填下方事件回调密钥，消息格式选择 array。</p>
  <form v-if="draft.onebot" class="panel config-form spaced" @submit.prevent="save">
   <div class="section-heading"><div><h2>一份连接，管理 QQ 私聊和群聊</h2><p>共享 NapCat 连接；模型、会话与人物记忆按范围隔离。</p></div><Settings2 :size="22" /></div>
   <div class="config-form-grid"><label class="config-check"><input v-model="draft.private_enabled" type="checkbox" />启用主人 QQ 私聊</label><label class="config-check"><input v-model="draft.enabled" type="checkbox" />启用已添加的 QQ 群</label><label>主人 QQ<input v-model="draft.owner_qq" :readonly="!!data.integrations.qq?.owner_qq" autocomplete="off" /></label><label>机器人 QQ<input v-model="draft.bot_qq" autocomplete="off" /></label><label>默认对话模型<select v-model="draft.profile_id"><option value="">沿用默认模型</option><option v-for="p in data.profiles" :key="p.id" :value="p.id">{{p.name}}</option></select></label><label>群原始消息保留天数<input v-model.number="draft.retention_days" type="number" min="1" max="365" required /></label><label class="config-check"><input v-model="draft.reply_mentioned" type="checkbox" />群内被 @ 时回复</label><label class="config-check"><input v-model="draft.collect_memory" type="checkbox" />默认提取群人物特点</label></div>
   <details class="config-details"><summary>连接地址与独立人物库</summary><p>使用一键连接后通常无需调整。群人物记忆需要下方独立向量库与嵌入模型。</p><div class="config-form-grid"><label>OneBot HTTP 地址<input v-model="draft.onebot.url" type="url" /></label><label>OneBot Token<input v-model="draft.onebot.token" type="password" autocomplete="new-password" /></label><label class="full">事件回调密钥<input v-model="draft.inbound.secret" type="password" autocomplete="new-password" /></label><label>群人物 Qdrant 地址<input v-model="draft.vector.url" type="url" /></label><label>Qdrant API Key<input v-model="draft.vector.api_key" type="password" autocomplete="new-password" /></label><label>嵌入服务 Base URL<input v-model="draft.embedding.url" type="url" /></label><label>嵌入模型 ID<input v-model="draft.embedding.model" /></label><label>嵌入 API Key<input v-model="draft.embedding.api_key" type="password" autocomplete="new-password" /></label></div><small>保留掩码沿用已保存密钥。关闭接入或权限会保留连接信息。</small></details>
   <p v-if="saveError" role="alert" class="form-error">{{saveError}}</p><div class="config-form-actions"><button class="button" :disabled="saving">{{saving?'保存中…':'保存 QQ 设置'}}</button><RouterLink to="/settings?tab=features" class="text-link">查看私聊 / 群聊权限</RouterLink></div>
  </form>
  <QQPrivateRhythm :settings="data.integrations.qq" @changed="load" />
  <QQGroups class="spaced" @changed="load" @filter="g=>router.push(`/qq?group=${g}`)" />
  <QQDecision :settings="data.integrations.qq?.decision" @changed="load" />
 </ResourceState>
</template>
