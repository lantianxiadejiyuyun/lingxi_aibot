<script setup lang="ts">
import { ref } from 'vue'
import { Plus, RefreshCw, Clock3, Trash2, ChevronLeft, ChevronRight } from '@lucide/vue'
import { api, patch, post, remove, session } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Job, Schedule } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
const offset = ref(0)
const statuses: Record<string,string> = { queued: '等待执行', running: '执行中', completed: '已完成', failed: '失败', cancelled: '已取消', needs_review: '需要核实' }
const actions: Record<string,string> = { bili_read: 'B 站字幕阅读', companion_tick: '主动陪伴', qq_private: 'QQ 主人私聊', qq_sticker: '图片收藏', notify: '提醒通知', reminders: '到期提醒', consolidate: '整理记忆', briefing: '生成简报', image: '图片生成', research: '资料研究', index: '更新索引', download: '下载文件', webhook: '发送通知', skill: '执行技能' }
const { data, loading, error, load } = useResource(async () => { const [jobs, schedules] = await Promise.all([api<Job[]>(`/jobs?limit=30&offset=${offset.value}`), api<Schedule[]>('/schedules')]); return { jobs, schedules } }, { jobs: [], schedules: [] } as { jobs: Job[]; schedules: Schedule[] })
function schedule() { openForm({ title: '安排一件定时的事', fields: [field('name', '计划名称', '', 'text', true), choice('action', '要做什么', [['notify','提醒我'],['reminders','检查到期事项'],['briefing','生成工作简报'],['consolidate','整理长期记忆']], 'notify'), { ...field('cron', '执行时间', '0 9 * * *', 'text', true), hint: 'Cron：分 时 日 月 周。默认每天 09:00。' }, field('timezone', '时区', session.user?.timezone || 'Asia/Shanghai', 'text', true), field('body', '提醒内容', '', 'textarea')], save: async v => { await post('/schedules', { name: v.name, action: v.action, cron: v.cron, timezone: v.timezone, params: { title: v.name, body: v.body }, enabled: true }); notify('定时计划已创建'); await load() } }) }
async function toggle(s: Schedule) { await patch(`/schedules/${s.id}`, { enabled: !s.enabled }); await load() }
function destroy(s: Schedule) { confirmAction('删除定时计划？', `“${s.name}”将停止安排新的任务。`, async () => { await remove(`/schedules/${s.id}`); await load() }) }
async function cancel(j: Job) { await post(`/jobs/${j.id}/cancel`); notify('已请求取消，已完成的外部操作无法撤回'); await load() }
function paginate(by: number) { offset.value += by * 30; load() }
</script>
<template>
  <PageHeading title="让事情，按时发生" description="定时计划和后台任务，都有迹可循。" eyebrow="THINGS IN MOTION"><button class="button secondary" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button><button class="button" @click="schedule"><Plus :size="17" />创建计划</button></PageHeading>
  <ResourceState :loading="loading" :error="error" @retry="load"><section class="panel"><div class="section-heading"><h2>定时计划 <span class="count-pill">{{ data.schedules.length }}</span></h2><Clock3 :size="18" /></div><EmptyState v-if="!data.schedules.length" title="把重复的事，交给时间" description="安排一个提醒，或定期整理你的记忆。" /><div v-for="s in data.schedules" :key="s.id" class="schedule-row"><div class="schedule-icon"><Clock3 :size="20" /></div><div class="grow"><strong>{{ s.name }}</strong><small>{{ s.cron }} · {{ s.timezone }}<br />{{ s.enabled ? '下次执行：' + date(s.next_run_at) : '计划已暂停' }}</small></div><button class="switch" :class="{ on: s.enabled }" role="switch" :aria-checked="s.enabled" :aria-label="s.name + '启用状态'" @click="run(() => toggle(s))"><span></span></button><button class="icon-button danger" :aria-label="'删除 ' + s.name" @click="destroy(s)"><Trash2 :size="16" /></button></div></section>
    <section class="panel spaced"><div class="section-heading"><h2>最近执行</h2><span class="muted small">点击任务查看详情</span></div><EmptyState v-if="!data.jobs.length" title="此刻，一切安静" description="有新的后台任务时，会在这里显示进度。" /><details v-for="j in data.jobs" :key="j.id" class="job-row"><summary><span :class="['status-label', j.status]"><i></i>{{ statuses[j.status] || j.status }}</span><strong>{{ actions[j.action] || j.action }}</strong><small>{{ date(j.created_at) }}</small><ChevronRight :size="16" /></summary><div class="job-detail"><p v-if="j.error" class="form-error">{{ j.error }}</p><pre>{{ j.result || '尚无执行结果' }}</pre><button v-if="['queued','running'].includes(j.status)" class="button secondary danger" @click="run(() => cancel(j))">取消任务</button></div></details></section>
    <div v-if="offset || data.jobs.length === 30" class="pagination"><button class="button secondary" :disabled="!offset" @click="paginate(-1)"><ChevronLeft :size="16" />上一页</button><span>第 {{ offset / 30 + 1 }} 页</span><button class="button secondary" :disabled="data.jobs.length < 30" @click="paginate(1)">下一页<ChevronRight :size="16" /></button></div>
  </ResourceState>
</template>
