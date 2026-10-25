<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import NotificationChannels from '../components/NotificationChannels.vue'
import FeaturePermissions from '../components/FeaturePermissions.vue'
import FeaturePreferences from '../components/FeaturePreferences.vue'
import QQSettings from '../components/QQSettings.vue'
import MoodSettings from '../components/MoodSettings.vue'
import InteractionSettings from '../components/InteractionSettings.vue'
import SettingsSection from '../components/SettingsSection.vue'
import GrowthSettings from '../components/GrowthSettings.vue'
import Companion from './Companion.vue'
import '../styles/settings-center.css'
import { Plus, Cpu, ArrowUpRight, Globe, Image, Brain, Bell, Download, HardDrive, KeyRound, UserRound, Pencil, Trash2, ShieldCheck, SlidersHorizontal } from '@lucide/vue'
import { api, patch, post, put, remove, requestId, session } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, type Field } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const section = ref('interaction')
const route = useRoute(), router = useRouter()
watch(() => route.query.tab, tab => { section.value = typeof tab === 'string' && ['interaction','features','models','qq','activities','integrations','channels','account'].includes(tab) ? tab : 'interaction' }, { immediate: true })
const tabs = [{key:'interaction',name:'日常相处',icon:Brain},{ key: 'features', name: '功能与权限', icon: ShieldCheck }, { key: 'qq', name: 'QQ 与群聊', icon: SlidersHorizontal }, { key: 'activities', name: '习惯与偏好', icon: Brain }, { key: 'models', name: '模型与人设', icon: Cpu }, { key: 'integrations', name: '服务连接', icon: SlidersHorizontal }, { key: 'channels', name: '飞书与通知', icon: Bell }, { key: 'account', name: '账户与访问', icon: UserRound }]
const primaryTabs=tabs.filter(t=>['interaction','models','qq','channels'].includes(t.key))
const extraTabs=tabs.filter(t=>!primaryTabs.includes(t))
const services = [
  { key: 'search', name: '联网搜索', description: '连接 SearXNG，获取最新信息', icon: Globe, tone: 'blue' },
  { key: 'image', name: '图片生成', description: '连接兼容接口，让想象成为画面', icon: Image, tone: 'sand' },
  { key: 'embedding', name: '语义检索', description: '通过嵌入模型，更准确地找到内容', icon: Brain, tone: 'lilac' },
  { key: 'webhook', name: '外部通知', description: '将通知送达飞书、OneBot 等渠道', icon: Bell, tone: 'green' },
  { key: 'aria2', name: 'aria2 下载', description: '通过 JSON-RPC 管理下载任务', icon: Download, tone: 'blue' },
  { key: 'qbittorrent', name: 'qBittorrent', description: '连接 Web API 管理种子下载', icon: HardDrive, tone: 'sand' },
]
interface SettingsData { profiles: Profile[]; integrations: Record<string, any>; tokens: { id: string; name: string; expires_at: number }[] }
const { data, loading, error, load } = useResource(async () => { const [profiles, integrations, tokens] = await Promise.all([['models','activities'].includes(section.value)?api<Profile[]>('/profiles'):Promise.resolve([]), section.value==='integrations'?api('/integrations'):Promise.resolve({}), section.value==='account'?api('/tokens'):Promise.resolve([])]); return { profiles, integrations, tokens } as SettingsData }, { profiles: [], integrations: {}, tokens: [] } as SettingsData)
watch(section,()=>{void load()})
function editProfile(p?: Profile) {
  const fields: Field[] = [field('name','配置名称', p?.name || '', 'text', true), choice('protocol','接口协议',[['openai','OpenAI 兼容'],['anthropic','Anthropic']],p?.protocol || 'openai'), { ...field('base_url','Base URL',p?.base_url || '', 'url', true), wide: true, hint: '包括版本前缀，例如 /v1；请填写你的模型服务地址。' }, field('model','模型 ID',p?.model || '', 'text', true), field('api_key','API Key',p?.api_key || '', 'password'), { ...field('context_window','上下文容量（tokens）',p?.context_window || 128000,'number',true), min: 2048, max: 4000000, step: '1', presets: [[8192,'8K'],[16384,'16K'],[32768,'32K'],[65536,'64K'],[128000,'128K'],[200000,'200K'],[256000,'256K'],[512000,'512K'],[1000000,'1M']], hint: '包含输入与输出。点选常用值，也可以手动填写；请以模型服务实际支持的容量为准。' }, { ...field('max_output','最大输出（tokens）',p?.max_output || 8192,'number',true), min: 128, max: 65536, step: '1', presets: [[1024,'1K'],[2048,'2K'],[4096,'4K'],[8192,'8K'],[16384,'16K'],[32768,'32K'],[65536,'64K']], hint: '推理模型的思考也可能占用输出额度。出现“输出达到长度限制”时应调整此项；仅增大上下文容量不能解决输出截断。' }, field('system_prompt','基础人设',p?.system_prompt || '', 'textarea')]
  fields.forEach(f=>{f.advanced=['protocol','context_window','max_output'].includes(f.key)})
  openForm({ title: p ? '编辑模型配置' : '连接一个对话模型', description: '配置后，就可以和灵犀开始对话。', fields, save: async v => {
    const model = { ...v, id: p?.id || requestId(), context_window: Number(v.context_window), max_output: Number(v.max_output) }
    if (!Number.isInteger(model.context_window) || !Number.isInteger(model.max_output) || model.context_window < 2048 || model.context_window > 4000000 || model.max_output < 128 || model.max_output > 65536 || model.max_output + 1024 >= model.context_window) throw new Error('上下文须为 2048–4000000，输出须为 128–65536，且上下文要比输出多留出至少 1025 tokens。')
    await put('/profiles', p ? data.value.profiles.map(q => q.id === p.id ? model : q) : [...data.value.profiles, model]); notify('模型配置已保存'); await load()
  } })
}
function deleteProfile(p: Profile) { confirmAction('移除这个模型？', '使用此配置的会话需要重新选择模型。', async () => { await put('/profiles', data.value.profiles.filter(q => q.id !== p.id)); await load() }) }
function integration(key: string) {
  const cfg = data.value.integrations[key] || {}, name = services.find(s => s.key === key)!.name
  const fields: Field[] = [{ ...field('url','服务 URL',cfg.url || '', 'url', true), wide: true }]
  if (['image','embedding'].includes(key)) fields.push(field('model','模型 ID',cfg.model || '', 'text', true), field('api_key','API Key',cfg.api_key || '', 'password'))
  if (['aria2','webhook'].includes(key)) fields.push(field('token','Token（可选）',cfg.token || '', 'password'))
  if (key === 'qbittorrent') fields.push(field('username','用户名',cfg.username || ''), field('password','密码',cfg.password || '', 'password'))
  if (key === 'webhook') fields.push(choice('kind','通知渠道',[['generic','通用 Webhook'],['feishu','飞书'],['onebot','OneBot']],cfg.kind || 'generic'), field('recipient','接收人 ID',cfg.recipient || ''))
  openForm({ title: '连接' + name, description: '保留掩码表示沿用已保存的密钥。', fields, save: async v => { const fresh=await api<Record<string,any>>('/integrations'); await put('/integrations', { ...fresh, [key]: v }); notify('服务配置已保存'); await load() } })
}
function disconnect(key: string) { confirmAction('断开服务连接？', '该服务将停止用于新的请求。', async () => { const cfg = await api<Record<string,any>>('/integrations'); delete cfg[key]; await put('/integrations', cfg); await load() }) }
function token() { openForm({ title: '创建访问 Token', description: '用于可信客户端接入你的灵犀账户。', fields: [field('name','名称','','text',true), { ...field('days','有效天数',30,'number',true), min: 1, max: 365 }], save: async v => { const result = await post<{token:string}>('/tokens', { name: v.name, days: Number(v.days) }); await load(); openForm({ title: '保存你的 Token', description: '仅显示这一次。复制后保存在安全的位置。', fields: [{ ...field('token','访问 Token',result.token,'textarea'), readonly: true }], submit: '我已保存', save: async () => {} }) } }) }
function revoke(id: string) { confirmAction('撤销这个 Token？', '相关客户端将需要重新登录或更换 Token。', async () => { await remove(`/tokens/${id}`); await load() }) }
function createUser() { openForm({ title: '创建一个新账户', fields: [field('username','用户名','','text',true), { ...field('password','登录密码','','password',true), hint: '至少 12 个字符。' }, choice('is_admin','角色',[['false','普通用户'],['true','管理员']],false)], save: async v => { await post('/users', { username: v.username, password: v.password, is_admin: v.is_admin === 'true' }); notify('账户已创建') } }) }
function timezone() { openForm({ title: '设置显示时区', fields: [field('timezone','时区',session.user?.timezone || 'Asia/Shanghai','text',true)], save: async v => { const user = await patch('/me', { timezone: v.timezone }); session.user = user; notify('时区已更新') } }) }
</script>
<template>
  <PageHeading title="设置中心" description="先选舒服的相处方式，再按需要连接和微调。" eyebrow="MAKE YOURSELF AT HOME" />
  <div class="settings-layout"><aside class="settings-tabs"><button v-for="t in primaryTabs" :key="t.key" :class="{ active: section === t.key }" :aria-pressed="section === t.key" @click="router.replace({ query: { ...route.query, tab: t.key } })"><component :is="t.icon" :size="18" />{{ t.name }}<ArrowUpRight :size="14" /></button><details class="settings-more" :open="extraTabs.some(t=>t.key===section)"><summary>更多设置</summary><button v-for="t in extraTabs" :key="t.key" :class="{active:section===t.key}" :aria-pressed="section===t.key" @click="router.replace({query:{tab:t.key}})"><component :is="t.icon" :size="18" />{{t.name}}<ArrowUpRight :size="14" /></button></details><div class="settings-note"><ShieldCheck :size="21" /><p>密钥加密保存，<br />个人数据归你所有。</p></div></aside><div class="settings-content"><ResourceState :loading="loading" :error="error" @retry="load">
    <InteractionSettings v-if="section === 'interaction'" /><FeaturePermissions v-else-if="section === 'features'" /><QQSettings v-else-if="section === 'qq'" /><section v-else-if="section === 'activities'"><div class="section-heading"><div><h2>习惯与偏好</h2><p>统一设置看图、阅读和主动陪伴的模型、频率与内容。</p></div></div><SettingsSection title="图片与 B 站" description="收藏偏好、阅读模型与时间" :initial-open="route.query.panel==='interests'"><FeaturePreferences /></SettingsSection><SettingsSection title="心情变化" description="轻微的情绪起伏，以及每个会话的近况" :initial-open="route.query.panel==='mood'"><MoodSettings :profiles="data.profiles" /></SettingsSection><SettingsSection title="主动陪伴与 AstrBot" description="什么时候可以找你，消息发到哪里" :initial-open="route.query.panel==='companion'"><Companion settings-mode /></SettingsSection></section><section v-else-if="section === 'models'" class="panel"><div class="section-heading"><div><h2>对话模型</h2><p>连接你喜欢的模型，开始与灵犀对话。</p></div><button class="button" @click="editProfile()"><Plus :size="16" />添加模型</button></div><EmptyState v-if="!data.profiles.length" title="第一步，给灵犀连接一个模型" description="支持 OpenAI 兼容接口和 Anthropic。填写服务地址、模型和密钥即可。" /><article v-for="p in data.profiles" :key="p.id" class="model-card"><span class="model-icon"><Cpu :size="24" /></span><div class="grow"><h3>{{ p.name }}</h3><p>{{ p.model }}</p><div class="model-tags"><span>{{ p.protocol === 'openai' ? 'OpenAI 兼容' : 'Anthropic' }}</span><span>{{ p.context_window.toLocaleString() }} 上下文</span><span>{{ p.max_output.toLocaleString() }} 最大输出</span></div></div><button class="icon-button" :aria-label="'编辑 ' + p.name" @click="editProfile(p)"><Pencil :size="16" /></button><button class="icon-button danger" :aria-label="'删除 ' + p.name" @click="deleteProfile(p)"><Trash2 :size="16" /></button></article><GrowthSettings :profiles="data.profiles" /></section>
    <section v-else-if="section === 'integrations'"><div class="section-heading"><div><h2>连接更多可能</h2><p>按需开启，让灵犀更得心应手。</p></div></div><div class="integration-grid"><article v-for="s in services" :key="s.key" class="integration-card"><div class="section-heading"><span :class="['stat-icon',s.tone]"><component :is="s.icon" :size="22" /></span><span :class="['tag', data.integrations[s.key]?.url ? 'green' : 'neutral']">{{ data.integrations[s.key]?.url ? '已配置' : '未连接' }}</span></div><h3>{{ s.name }}</h3><p>{{ s.description }}</p><div class="section-heading"><button class="text-link" @click="integration(s.key)">{{ data.integrations[s.key]?.url ? '管理连接' : '连接服务' }}<ArrowUpRight :size="15" /></button><button v-if="data.integrations[s.key]?.url" class="text-link danger" @click="disconnect(s.key)">断开</button></div></article></div></section>
    <section v-else-if="section === 'channels'"><NotificationChannels /></section><div v-else><section class="panel"><div class="section-heading"><h2>个人账户</h2><UserRound :size="19" /></div><div class="account-profile"><span class="user-avatar large">{{ session.user?.username[0]?.toUpperCase() }}</span><div class="grow"><h3>{{ session.user?.username }}</h3><p>{{ session.user?.is_admin ? '管理员' : '普通用户' }} · {{ session.user?.timezone }}</p></div><button class="button secondary" @click="timezone">设置时区</button></div></section><section class="panel spaced"><div class="section-heading"><div><h2>访问 Token</h2><p>连接可信的客户端与渠道。</p></div><button class="button secondary" @click="token"><Plus :size="15" />创建</button></div><div v-for="t in data.tokens" :key="t.id" class="token-row"><KeyRound :size="19" /><div class="grow"><strong>{{ t.name }}</strong><small>有效至 {{ date(t.expires_at) }}</small></div><button class="text-link danger" @click="revoke(t.id)">撤销</button></div></section><section v-if="session.user?.is_admin" class="panel spaced"><div class="section-heading"><div><h2>账户管理</h2><p>为新的成员创建独立账户。</p></div><button class="button secondary" @click="createUser"><Plus :size="15" />创建账户</button></div></section></div>
  </ResourceState></div></div>
</template>
