<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Search, ArrowUpRight, Pencil, Trash2, Check, Play, Clock3, ChevronLeft, ChevronRight, MapPin } from '@lucide/vue'
import { api, post, put, remove } from '../lib/api'
import { confirmAction, date, notify, recordBody, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import { editRecord, generateImage } from '../lib/records'
import { label, navigation } from '../lib/navigation'
import type { Entry, Page } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
import AssetImage from '../components/AssetImage.vue'
const props = defineProps<{ kind: string }>(), route = useRoute()
const search = ref(''), query = ref(''), offset = ref(0), status = ref(''), selectedDate = ref(String(route.query.date || ''))
const descriptions: Record<string,string> = { tasks: '把大计划拆成小步骤，每一次完成都算数。', events: '给重要的事，留一个位置。', notes: '留住灵光一现，也记录认真生活的瞬间。', expenses: '看清每一笔花费，让生活心中有数。', fitness: '你的每一份坚持，都在这里留下痕迹。', trips: '从日常出发，去见新的风景。', pages: '把想法做成作品，分享你的创造。', images: '让想象生长，让画面说话。', skills: '把重复的动作，变成一次轻松的调用。', bookmarks: '收藏值得再看的内容。' }
const { data, loading, error, load } = useResource(async () => {
  const path = `/records/${props.kind}?limit=${selectedDate.value && props.kind === 'events' ? 200 : 24}&offset=${offset.value}&q=${encodeURIComponent(query.value)}${status.value ? '&status=' + status.value : ''}`
  const result = await api<Page<Entry>>(path)
  if (props.kind === 'events' && selectedDate.value) {
    // Read all pages before filtering a day, so older calendar entries cannot disappear.
    let more = result.has_more, after = 200
    while (more) { const batch = await api<Page<Entry>>(`/records/events?limit=200&offset=${after}&q=${encodeURIComponent(query.value)}`); result.items.push(...batch.items); more = batch.has_more; after += 200 }
    result.items = result.items.filter(e => { const start = new Date(selectedDate.value + 'T00:00:00').getTime() / 1000; return e.data.start_at < start + 86400 && e.data.end_at >= start })
    result.has_more = false
  }
  return result
}, { items: [], has_more: false } as Page<Entry>)
const icon = computed(() => navigation.find(n => n.id === props.kind)?.icon)
watch([status, selectedDate], () => { offset.value = 0; load() })
function find() { query.value = search.value; offset.value = 0; load() }
function paginate(by: number) { offset.value += by * 24; load() }
function add() { if (props.kind === 'images') generateImage(); else editRecord(props.kind, load) }
async function complete(r: Entry) { await put(`/records/${props.kind}/${r.id}`, recordBody(r, { status: r.status === 'done' ? 'open' : 'done' })); await load() }
function destroy(r: Entry) { confirmAction('删除这条记录？', `“${r.title}”将从你的空间中移除。`, async () => { await remove(`/records/${props.kind}/${r.id}`); notify('记录已删除'); await load() }) }
function execute(r: Entry) { confirmAction('运行这个技能？', '将执行你已启用的全部步骤，可在任务中心查看结果。', async () => { await post(`/skills/${r.id}/run`); notify('技能任务已提交') }) }
function safeLink(url: unknown) { try { const u = new URL(String(url)); return ['https:', 'http:'].includes(u.protocol) ? u.href : undefined } catch { return undefined } }
</script>
<template>
  <PageHeading :title="label(kind)" :description="descriptions[kind]" eyebrow="LITTLE THINGS, WELL KEPT"><button class="button" @click="add"><Plus :size="17" />{{ kind === 'images' ? '生成图片' : '新建记录' }}</button></PageHeading>
  <div class="list-toolbar"><div v-if="kind === 'tasks'" class="segmented"><button v-for="[value,name] in [['','全部事项'],['open','待完成'],['done','已完成']]" :key="value" :class="{ active: status === value }" @click="status = value">{{ name }}</button></div><span v-else class="section-label"><component :is="icon" :size="17" />我的{{ label(kind) }}</span><div class="toolbar-right"><label v-if="kind === 'events'" class="date-filter"><span class="sr-only">筛选日期（浏览器本地时区）</span><input v-model="selectedDate" type="date" /></label><form class="search-field" @submit.prevent="find"><Search :size="17" /><input v-model="search" type="search" :placeholder="'搜索' + label(kind)" aria-label="搜索记录" /><button v-if="search" class="text-link">搜索</button></form></div></div>
  <ResourceState :loading="loading" :error="error" @retry="load"><EmptyState v-if="!data.items.length" :title="query || status || selectedDate ? '没有找到匹配的记录' : '你的故事，从这里开始'" description="写下一条记录，让日常的小事井井有条。"><button class="button secondary" @click="add"><Plus :size="16" />添加第一条记录</button></EmptyState>
    <div v-else :class="['record-grid', { 'task-list': kind === 'tasks' }]">
      <article v-for="(record, i) in data.items" :key="record.id" :class="['record-card', { completed: record.status === 'done', 'note-card': kind === 'notes' }, kind === 'notes' ? 'note-tone-' + i % 3 : '']">
        <button v-if="kind === 'tasks'" class="task-check" :class="{ checked: record.status === 'done' }" :aria-label="(record.status === 'done' ? '重新打开 ' : '完成 ') + record.title" @click="run(() => complete(record))"><Check :size="15" /></button>
        <AssetImage v-if="kind === 'images'" :id="record.id" :alt="record.title" />
        <div class="record-main"><div class="record-meta"><span v-if="kind === 'expenses'" class="expense-amount">¥ {{ (record.data.amount_cents / 100).toFixed(2) }}</span><span v-else-if="kind === 'notes'" class="note-symbol"><component :is="icon" :size="19" /></span><span v-else :class="['tag', kind === 'tasks' && record.data.priority === 3 ? 'warm' : 'neutral']">{{ record.status === 'done' ? '已完成' : kind === 'tasks' ? ['低优先级','普通','高优先级'][(record.data.priority || 2)-1] : kind === 'skills' ? record.data.enabled ? '已启用' : '未启用' : label(kind) }}</span><small>{{ date(record.updated_at, true) }}</small></div><h3>{{ record.title }}</h3><p class="record-preview">{{ kind === 'pages' ? 'HTML 网页作品' : record.content || '暂无备注，随时添上新想法。' }}</p><div v-if="record.due_at || record.data.start_at || record.data.date" class="record-info"><Clock3 :size="13" />{{ date(record.due_at || record.data.start_at) === '未设置' ? record.data.date : date(record.due_at || record.data.start_at) }}</div><div v-if="record.data.location" class="record-info"><MapPin :size="13" />{{ record.data.location }}</div><div v-if="record.data.tags?.length" class="record-tags"><span v-for="tag in record.data.tags" :key="tag"># {{ tag }}</span></div></div>
        <div class="record-actions"><a v-if="kind === 'pages' && record.data.public" :href="'/p/' + record.id" target="_blank" rel="noopener" class="text-link">打开网页<ArrowUpRight :size="14" /></a><a v-if="kind === 'bookmarks' && safeLink(record.data.url)" :href="safeLink(record.data.url)" target="_blank" rel="noopener noreferrer" class="text-link">打开链接<ArrowUpRight :size="14" /></a><button v-if="kind === 'skills' && record.data.enabled" class="text-link" @click="execute(record)"><Play :size="14" />运行</button><button class="icon-button" :aria-label="'编辑 ' + record.title" @click="editRecord(kind, load, record)"><Pencil :size="16" /></button><button class="icon-button danger" :aria-label="'删除 ' + record.title" @click="destroy(record)"><Trash2 :size="16" /></button></div>
      </article>
    </div>
    <div v-if="offset || data.has_more" class="pagination"><button class="button secondary" :disabled="!offset" @click="paginate(-1)"><ChevronLeft :size="16" />上一页</button><span>第 {{ offset / 24 + 1 }} 页</span><button class="button secondary" :disabled="!data.has_more" @click="paginate(1)">下一页<ChevronRight :size="16" /></button></div>
  </ResourceState>
</template>
