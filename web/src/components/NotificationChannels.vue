<script setup lang="ts">
import { Bell, ArrowUpRight, Settings2 } from '@lucide/vue'
import { api, put } from '../lib/api'
import { useResource } from '../lib/resource'
import { choice, field, notify, openForm, run, type Field } from '../lib/ui'
import FeishuConnect from './FeishuConnect.vue'
import ResourceState from './ResourceState.vue'
const services = [{id:'feishu', name:'飞书', description:'自定义机器人 Webhook，支持签名。'}, {id:'feishu_app', name:'飞书机器人（应用）', description:'使用应用身份，发送到指定用户或群。'}, {id:'serverchan', name:'Server酱', description:'支持 Turbo / Server酱³ SendKey。'}]
const { data, loading, error, load } = useResource(() => api<Record<string,any>>('/notification-channels'), {})
function configured(id: string) { const c = data.value[id] || {}; return id === 'feishu' ? !!c.url : id === 'feishu_app' ? !!(c.app_id && c.secret && c.recipient) : !!c.secret }
async function configure(id: string) {
  const latest = await api<Record<string,any>>('/notification-channels'), c = latest[id] || {}
  let fields: Field[]
  if (id === 'feishu') fields = [field('url','机器人 Webhook URL',c.url || '', 'url',true),field('secret','签名密钥（可选）',c.secret || '', 'password')]
  else if (id === 'feishu_app') fields = [field('app_id','App ID',c.app_id || '', 'text',true),field('secret','App Secret',c.secret || '', 'password',true),choice('recipient_type','接收人类型',[['open_id','用户 open_id'],['user_id','用户 user_id'],['union_id','用户 union_id'],['chat_id','会话 chat_id']],c.recipient_type || 'open_id'),{...field('recipient','通知接收人 ID（可选）',c.recipient || '', 'text'),hint:'只使用双向私聊时可留空，身份通过绑定码确认。'}]
  else fields = [field('secret','SendKey',c.secret || '', 'password',true)]
  openForm({title:`连接${services.find(s=>s.id===id)?.name}`,description:'保留掩码沿用密钥。保存只更新配置，不发送测试消息。',fields,save:async v=>{await put('/notification-channels',{...latest,[id]:v});notify('通知渠道已保存');await load()}})
}
</script>
<template>
  <div class="section-heading"><div><h2>通知渠道</h2><p>主动消息会发送到你选择的接收人，请按需配置。</p></div><Bell :size="21" /></div>
  <ResourceState :loading="loading" :error="error" @retry="load"><div class="integration-grid"><article v-for="s in services" :key="s.id" class="integration-card"><div class="section-heading"><span class="stat-icon green"><Bell :size="22" /></span><span :class="['tag', configured(s.id) ? 'green' : 'neutral']">{{ configured(s.id) ? '已配置' : '待配置' }}</span></div><h3>{{ s.name }}</h3><p>{{ s.description }}</p><button class="text-link" @click="run(()=>configure(s.id))"><Settings2 :size="15" />{{ configured(s.id) ? '管理连接' : '连接服务' }}</button></article></div><div class="panel spaced"><h3>站内通知、QQ 与 AstrBot</h3><p class="muted small">站内通知始终可用。QQ 私聊与 AstrBot 的绑定入口在主动陪伴页。</p><RouterLink to="/companion" class="text-link">前往主动陪伴<ArrowUpRight :size="15" /></RouterLink></div><FeishuConnect /></ResourceState>
</template>
