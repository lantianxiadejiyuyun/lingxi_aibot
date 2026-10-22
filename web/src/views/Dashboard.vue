<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUpRight, ArrowRight, Plus, Check, CheckCheck, NotebookPen, Brain, Bell, Sunrise, Clock3, Sparkles } from '@lucide/vue'
import { api, put, session } from '../lib/api'
import { date, run, recordBody } from '../lib/ui'
import { useResource } from '../lib/resource'
import { editRecord } from '../lib/records'
import type { Entry, Page, Stats } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
import BrandMark from '../components/BrandMark.vue'
import MiniCalendar from '../components/MiniCalendar.vue'
const { data, loading, error, load } = useResource(async () => {
  const [stats, tasks, notes, events] = await Promise.all([api<Stats>('/dashboard'), api<Page<Entry>>('/records/tasks?status=open&limit=5'), api<Page<Entry>>('/records/notes?limit=3'), api<Page<Entry>>('/records/events?limit=100')])
  return { stats, tasks: tasks.items, notes: notes.items, events: events.items.filter(e => e.data.end_at >= Date.now() / 1000).sort((a,b) => a.data.start_at - b.data.start_at).slice(0,3) }
}, { stats: { pending_tasks: 0, memories: 0, unread_notifications: 0, counts: {} } as Stats, tasks: [] as Entry[], notes: [] as Entry[], events: [] as Entry[] })
const stats = computed(() => [
  { value: data.value.stats.pending_tasks, name: '待完成事项', note: '一步一步，慢慢来', icon: CheckCheck, tone: 'green', path: '/tasks' },
  { value: data.value.stats.counts.notes || 0, name: '灵感与笔记', note: '捕捉每个好想法', icon: NotebookPen, tone: 'sand', path: '/notes' },
  { value: data.value.stats.memories, name: '长期记忆', note: '让每次对话更懂你', icon: Brain, tone: 'lilac', path: '/memories' },
  { value: data.value.stats.unread_notifications, name: '未读通知', note: '重要的事，及时知道', icon: Bell, tone: 'blue', path: '/notifications' },
])
async function complete(r: Entry) { await put(`/records/tasks/${r.id}`, recordBody(r, { status: 'done' })); await load() }
</script>
<template>
  <div class="dashboard-view">
    <PageHeading :title="`你好，${session.user?.username}。`" description="生活不必事事匆忙，今天也按自己的节奏来。" eyebrow="MAKE ROOM FOR WHAT MATTERS"><button class="button secondary" @click="editRecord('tasks', load)"><Plus :size="17" />记录一件事</button></PageHeading>
    <ResourceState :loading="loading" :error="error" @retry="load">
      <section class="welcome-banner"><div class="welcome-copy"><span class="banner-tag"><span></span>你的灵犀，随时在这里</span><h2>把琐事交给灵犀，<br />把时间留给<span>自己。</span></h2><p>整理想法、安排日程，或只是聊聊今天。<br class="desktop-break" />从一个小小的念头，开始更从容的一天。</p><RouterLink to="/chat" class="button banner-button">和灵犀聊一聊<ArrowUpRight :size="18" /></RouterLink></div><div class="banner-art" aria-hidden="true"><div class="art-orbit orbit-a"></div><div class="art-orbit orbit-b"></div><div class="art-orbit orbit-c"></div><div class="floating-note note-one"><Check :size="15" /><span>少一点琐碎</span></div><div class="art-sphere"><BrandMark /></div><div class="floating-note note-two"><Sparkles :size="15" /><span>多一点灵感</span></div><span class="art-dot dot-one"></span><span class="art-dot dot-two"></span><span class="art-caption">a little clarity, every day.</span></div></section>
      <div class="stats-grid"><RouterLink v-for="s in stats" :key="s.name" :to="s.path" class="stat-card"><div class="stat-top"><span :class="['stat-icon', s.tone]"><component :is="s.icon" :size="20" /></span><ArrowUpRight :size="16" /></div><div class="stat-value">{{ s.value }}<span>{{ s.name }}</span></div><small>{{ s.note }}</small></RouterLink></div>
      <div class="dashboard-columns"><div class="dashboard-main">
        <section class="panel task-panel"><div class="section-heading"><h2>接下来要做 <span class="count-pill">{{ data.stats.pending_tasks }}</span></h2><RouterLink to="/tasks" class="text-link">查看全部<ArrowRight :size="15" /></RouterLink></div><div v-for="task in data.tasks" :key="task.id" class="task-row"><button class="task-check" :aria-label="'完成 ' + task.title" @click="run(() => complete(task))"><Check :size="14" /></button><div class="task-detail"><strong>{{ task.title }}</strong><span><Clock3 :size="12" />{{ task.due_at ? date(task.due_at) : '按自己的节奏来' }}<template v-if="task.data.project"> · {{ task.data.project }}</template></span></div><span :class="['tag', task.data.priority === 3 ? 'warm' : 'neutral']">{{ ['低优先级', '普通', '高优先级'][(task.data.priority || 2) - 1] }}</span></div><EmptyState v-if="!data.tasks.length" title="此刻，没有待办牵挂" description="记下下一件小事，或者享受一下这份轻松。" /><button class="add-row" @click="editRecord('tasks', load)"><Plus :size="16" />添加一个小目标</button></section>
        <section class="panel notes-panel"><div class="section-heading"><h2>最近的灵感</h2><RouterLink to="/notes" class="text-link">全部笔记<ArrowRight :size="15" /></RouterLink></div><div v-if="data.notes.length" class="note-preview-grid"><RouterLink v-for="(note,i) in data.notes" :key="note.id" to="/notes" :class="['note-preview', 'note-tone-' + i]"><NotebookPen :size="18" /><h3>{{ note.title }}</h3><p>{{ note.content || '一个值得记住的想法。' }}</p><small>{{ date(note.updated_at, true) }}<ArrowUpRight :size="15" /></small></RouterLink></div><EmptyState v-else title="好想法，值得被留下" description="写下一句话，把稍纵即逝的灵感收藏起来。"><button class="text-link" @click="editRecord('notes', load)">写第一篇笔记<ArrowUpRight :size="15" /></button></EmptyState></section>
      </div><aside class="dashboard-side"><MiniCalendar /><section class="panel agenda-panel"><div class="section-heading"><h2>即将到来的日程</h2><RouterLink class="icon-button" to="/events" aria-label="查看日程"><ArrowUpRight :size="16" /></RouterLink></div><div v-for="event in data.events" :key="event.id" class="agenda-item"><span class="agenda-line"></span><div><small>{{ date(event.data.start_at) }}</small><strong>{{ event.title }}</strong><span v-if="event.data.location">{{ event.data.location }}</span></div></div><div v-if="!data.events.length" class="agenda-empty"><Sunrise :size="25" /><p>接下来，一片从容。</p><RouterLink to="/events">为日历添一笔<ArrowRight :size="14" /></RouterLink></div></section><div class="quiet-card"><BrandMark /><p>把脑海里的事放下来，<br />给新的灵感腾点地方。</p><RouterLink to="/memories">看看灵犀记住了什么<ArrowUpRight :size="14" /></RouterLink></div></aside></div>
    </ResourceState>
  </div>
</template>
