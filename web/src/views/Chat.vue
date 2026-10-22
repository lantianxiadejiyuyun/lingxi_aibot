<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { Plus, MessageSquare, ArrowUp, Square, ChevronDown, PanelLeftClose, PanelLeftOpen, Trash2, Minimize2, Sparkles, Check, ArrowUpRight, NotebookPen, CalendarDays, Brain, LoaderCircle } from '@lucide/vue'
import { api, authHeaders, patch, post, remove, requestId, session } from '../lib/api'
import { confirmAction, date, notify, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import { readEvents } from '../lib/stream'
import type { ChatMessage, Conversation, Profile } from '../types'
import BrandMark from '../components/BrandMark.vue'
import ResourceState from '../components/ResourceState.vue'
import MessageBody from '../components/MessageBody.vue'
const conversations = ref<Conversation[]>([]), profiles = ref<Profile[]>([]), selected = ref(''), profile = ref('')
const messages = ref<ChatMessage[]>([]), input = ref(''), busy = ref(false), historyOpen = ref(window.matchMedia('(min-width: 981px)').matches), historyLoading = ref(false)
const messageBox = ref<HTMLElement>(), inputBox = ref<HTMLTextAreaElement>()
let controller: AbortController | null = null, historySequence = 0
const suggestions = [
  { icon: CalendarDays, title: '理清今天的计划', text: '帮我整理今天的待办事项，按优先级安排一下。', description: '让今天，心中有数' },
  { icon: NotebookPen, title: '捕捉一个好想法', text: '我有一个新想法，帮我梳理并记下来。', description: '把灵感，留在这里' },
  { icon: Brain, title: '多了解我一点', text: '记住：我喜欢简洁、有条理的回答。', description: '越聊，越有默契' },
]
const { loading, error, load } = useResource(async () => {
  const [c, p] = await Promise.all([api<Conversation[]>('/conversations?limit=100'), api<Profile[]>('/profiles')])
  conversations.value = c; profiles.value = p; profile.value = p[0]?.id || ''
  return true
}, false)
const current = computed(() => conversations.value.find(c => c.id === selected.value))
const modelName = computed(() => profiles.value.find(p => p.id === profile.value)?.model)
async function scroll() { await nextTick(); messageBox.value?.scrollTo({ top: messageBox.value.scrollHeight, behavior: 'instant' }) }
async function select(id: string) {
  if (busy.value) return
  const sequence = ++historySequence
  selected.value = id; messages.value = []; historyLoading.value = true
  profile.value = conversations.value.find(c => c.id === id)?.profile_id || profiles.value[0]?.id || ''
  try {
    let after = 0
    while (true) {
      const batch = await api<ChatMessage[]>(`/conversations/${id}/messages?after=${after}&limit=500`)
      if (sequence !== historySequence) return
      messages.value.push(...batch.filter(m => ['user', 'assistant'].includes(m.role)))
      if (batch.length < 500) break
      after = batch.at(-1)!.id
    }
    await scroll()
  } finally { if (sequence === historySequence) historyLoading.value = false }
}
function fresh() { if (busy.value) return; historySequence++; historyLoading.value = false; selected.value = ''; messages.value = []; input.value = ''; nextTick(() => inputBox.value?.focus()) }
async function changeProfile() { if (current.value) { await patch(`/conversations/${selected.value}`, { title: current.value.title, profile_id: profile.value }); current.value.profile_id = profile.value } }
function destroy() { if (!selected.value || busy.value) return; confirmAction('删除这个对话？', '对话及其全部消息将被删除。', async () => { await remove(`/conversations/${selected.value}`); conversations.value = conversations.value.filter(c => c.id !== selected.value); fresh() }) }
async function compact() { if (!selected.value) return; await post(`/conversations/${selected.value}/compact`); notify('上下文已整理，原始消息仍保留') }
async function send() {
  const text = input.value.trim()
  if (!text || busy.value || historyLoading.value || !profile.value) return
  busy.value = true
  const abort = new AbortController(); controller = abort
  let answer: ChatMessage | undefined
  try {
    if (!selected.value) { const c = await post<Conversation>('/conversations', { title: Array.from(text).slice(0, 30).join(''), profile_id: profile.value }); conversations.value.unshift(c); selected.value = c.id }
    input.value = ''
    messages.value.push({ id: -Date.now(), role: 'user', content: text }, { id: -Date.now() - 1, role: 'assistant', content: '', tools: [] })
    answer = messages.value.at(-1)!
    await scroll()
    const response = await fetch(`/api/v2/conversations/${selected.value}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify({ message: text, request_id: requestId() }), signal: abort.signal })
    if (!response.ok) { const e = await response.json().catch(() => ({})); throw Error(e.error || '无法发送消息，请重试') }
    if (!response.body) throw Error('浏览器不支持流式响应')
    await readEvents(response.body, event => {
      if (event.type === 'delta') answer!.content += event.data
      if (event.type === 'tool') answer!.tools!.push(event.data)
      if (event.type === 'notice' || event.type === 'error') notify(String(event.data), event.type === 'error')
      if (event.type === 'done' && event.data.reply) answer!.content = event.data.reply
      if (messageBox.value && messageBox.value.scrollHeight - messageBox.value.scrollTop - messageBox.value.clientHeight < 180) scroll()
    })
  } catch (e) {
    const message = e instanceof Error && e.name === 'AbortError' ? '已停止生成' : e instanceof Error ? e.message : '发送失败'
    notify(message, !(e instanceof Error && e.name === 'AbortError'))
    if (answer && !answer.content) answer.content = message
    if (!answer) input.value = text
  } finally { busy.value = false; controller = null }
}
function keydown(e: KeyboardEvent) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() } }
onBeforeUnmount(() => { historySequence++; controller?.abort() })
</script>
<template>
  <ResourceState :loading="loading" :error="error" @retry="load">
    <div class="chat-workspace" :class="{ 'history-hidden': !historyOpen }">
      <aside class="chat-history"><div class="history-heading"><span>我的对话</span><button class="icon-button" aria-label="收起对话列表" @click="historyOpen = false"><PanelLeftClose :size="17" /></button></div><button class="button new-conversation" :disabled="busy" @click="fresh"><Plus :size="17" />开启新对话</button><div class="history-caption">最近对话</div><div class="conversation-list"><button v-for="c in conversations" :key="c.id" class="conversation-item" :class="{ selected: selected === c.id }" :disabled="busy" @click="run(() => select(c.id))"><MessageSquare :size="16" /><span><strong>{{ c.title }}</strong><small>{{ date(c.updated_at, true) }}</small></span></button><p v-if="!conversations.length" class="history-empty">每一个好想法，<br />都可以从一次对话开始。</p></div><RouterLink to="/memories" class="history-memory"><Brain :size="18" /><span>灵犀的记忆<small>那些值得记住的事</small></span><ArrowUpRight :size="15" /></RouterLink></aside>
      <section class="chat-main"><div class="chat-toolbar"><div class="inline-actions"><button v-if="!historyOpen" class="icon-button" aria-label="展开对话列表" @click="historyOpen = true"><PanelLeftOpen :size="18" /></button><span class="chat-status"><span class="status-dot"></span>灵犀助手</span><span class="chat-separator">/</span><label class="model-select"><span class="sr-only">对话模型</span><select v-model="profile" :disabled="busy || historyLoading" @change="run(changeProfile)"><option v-if="!profiles.length" value="">尚未配置模型</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select><ChevronDown :size="13" /></label></div><div class="inline-actions"><button class="icon-button" :disabled="!selected || busy || historyLoading" title="整理上下文" aria-label="整理上下文" @click="run(compact)"><Minimize2 :size="17" /></button><button class="icon-button danger" :disabled="!selected || busy || historyLoading" title="删除对话" aria-label="删除对话" @click="destroy"><Trash2 :size="17" /></button></div></div>
        <div ref="messageBox" class="messages" aria-live="polite" :aria-busy="busy || historyLoading"><div v-if="historyLoading" class="loading-state"><LoaderCircle class="spin" />正在打开对话…</div><div v-else-if="!messages.length" class="chat-welcome"><div class="welcome-logo"><BrandMark /></div><span class="eyebrow">THINK A LITTLE FREER</span><h1>想法、计划，<br />或者只是<span>聊聊今天。</span></h1><p>我是灵犀。随时倾听，帮你把事情理清。</p><div class="suggestion-grid"><button v-for="s in suggestions" :key="s.title" @click="input = s.text; inputBox?.focus()"><component :is="s.icon" :size="19" /><strong>{{ s.title }}</strong><small>{{ s.description }}</small><ArrowUpRight :size="15" class="suggestion-arrow" /></button></div></div>
          <article v-for="m in messages" :key="m.id" :class="['message', m.role]"><div class="message-avatar"><BrandMark v-if="m.role === 'assistant'" /><span v-else>{{ session.user?.username[0]?.toUpperCase() }}</span></div><div class="message-content"><div class="message-author">{{ m.role === 'user' ? '你' : '灵犀' }}<span v-if="m.role === 'assistant'">AI</span></div><div class="message-body"><MessageBody v-if="m.role === 'assistant' && m.content" :text="m.content" /><span v-else>{{ m.content }}</span><span v-if="!m.content && busy && m === messages.at(-1)" class="typing-dots"><i></i><i></i><i></i></span></div><details v-for="(tool,i) in m.tools" :key="i" class="tool-result"><summary><Check :size="14" />{{ tool.name }}<ChevronDown :size="13" /></summary><pre>{{ JSON.stringify(tool.result, null, 2) }}</pre></details></div></article>
        </div>
        <div class="composer-area"><div v-if="!profiles.length" class="model-notice"><Sparkles :size="16" /><span>连接一个模型，开启你的第一段对话。</span><RouterLink to="/settings">去配置<ArrowUpRight :size="14" /></RouterLink></div><form class="composer" @submit.prevent="send"><textarea ref="inputBox" v-model="input" aria-label="消息内容" placeholder="把想法说给灵犀听…" rows="2" :disabled="historyLoading" @keydown="keydown"></textarea><div class="composer-bottom"><span><Sparkles :size="14" />{{ modelName || '你的个人 AI 助手' }}</span><div class="inline-actions"><small class="send-hint">Enter 发送 · Shift + Enter 换行</small><button v-if="busy" type="button" class="send-button stop" aria-label="停止生成" @click="controller?.abort()"><Square :size="15" /></button><button v-else class="send-button" aria-label="发送消息" :disabled="!input.trim() || !profile || historyLoading"><ArrowUp :size="20" /></button></div></div></form><p class="composer-note">灵犀也可能出错，重要信息请再确认一下。</p></div>
      </section>
    </div>
  </ResourceState>
</template>
