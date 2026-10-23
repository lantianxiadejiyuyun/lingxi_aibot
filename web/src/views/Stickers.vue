<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Sticker, Sparkles, Heart, Search, Settings2, Download, Pencil, Trash2, RefreshCw, ArrowUpRight, X, Image, Eye, MessageSquare, ChevronLeft, ChevronRight } from '@lucide/vue'
import { api, authHeaders, patch, post, put, remove } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
import EmptyState from '../components/EmptyState.vue'
import StickerImage from '../components/StickerImage.vue'
import '../styles/stickers.css'

interface Settings { enabled: boolean; auto_review: boolean; profile_id: string | null; taste: string; hourly_limit: number; max_items: number; max_megabytes: number }
interface Item { id: string; title: string; tags: string[]; reason: string; collected_by: string; favorite: boolean; mime: string; width: number; height: number; byte_size: number; group_id: string; scope: 'owner_private' | 'group'; occurrences: number; created_at: number }
interface Collection { items: Item[]; total: number; page: number; qq_enabled: boolean; private_enabled: boolean; settings: Settings; stats: { count: number; bytes: number; favorites: number; pending: number; failed: number }; recent: {status: string; reason: string; time: number}[] }
const defaults: Settings = { enabled: false, auto_review: true, profile_id: null, taste: '', hourly_limit: 20, max_items: 500, max_megabytes: 256 }
const search = ref(''), query = ref(''), favoriteOnly = ref(false), page = ref(1), selected = ref<Item | null>(null), dialog = ref<HTMLDialogElement>(), downloading = ref(false)
const initial: Collection = { items: [], total: 0, page: 1, qq_enabled: false, private_enabled: false, settings: defaults, stats: {count: 0, bytes: 0, favorites: 0, pending: 0, failed: 0}, recent: [] }
const { data, loading, error, load } = useResource(() => api<Collection>(`/qq/stickers?q=${encodeURIComponent(query.value)}&favorite=${favoriteOnly.value}&page=${page.value}`), initial)
let poll: ReturnType<typeof setInterval>
onMounted(() => { poll = setInterval(() => { if (data.value.stats.pending && !loading.value && !selected.value) void load() }, 10000) })
onBeforeUnmount(() => clearInterval(poll))
watch(selected, async item => { if (item) { await nextTick(); dialog.value?.showModal() } else dialog.value?.close() })
function filter(favorite = favoriteOnly.value) { query.value = search.value; favoriteOnly.value = favorite; page.value = 1; void load() }
function turn(delta: number) { page.value += delta; void load() }
function bytes(size: number) { return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB` }
function sourceLabel(item: Item) { return item.scope === 'owner_private' ? '主人私聊' : `群 ${item.group_id}` }
const statuses: Record<string, string> = { saved: '已收藏', rejected: '没有选中', failed: '处理失败', skipped: '已跳过', expanded: '正在收藏引用图片' }
async function configure() {
  const profiles = await api<Profile[]>('/profiles'), c = data.value.settings
  openForm({ title: '让灵犀挑选喜欢的图片', description: '支持主人私聊和已接入的群。模型看过喜欢才收藏，也可以由主人明确要求；聊天时会按需要从当前会话的收藏中挑一张回复。', fields: [
    choice('enabled', '图片收藏', [['true','开启'], ['false','关闭']], c.enabled),
    choice('auto_review', '收藏方式', [['true','模型挑选 + 主人命令'], ['false','仅主人命令']], c.auto_review),
    {...choice('profile_id', '看图模型', [['','沿用 QQ 对话模型'], ...profiles.map(p => [p.id, p.name] as [string,string])], c.profile_id || ''), wide: true, hint: '用于私聊识图和收藏判断，需支持图片输入。关闭收藏后，私聊仍可看图；GIF 最多识别 10 帧，不足 10 帧读取全部帧。'},
    {...field('taste', '灵犀的选图偏好', c.taste, 'textarea'), hint: '例如：喜欢可爱的小动物、简洁的画面和有趣的聊天表情。'},
    {...field('hourly_limit', '每小时最多判断收藏', c.hourly_limit, 'number', true), min: 1, max: 200, hint: '仅限制自动收藏判断；主人明确命令不占此额度。'},
    {...field('max_items', '每个图库最多收藏张数', c.max_items, 'number', true), min: 1, max: 2000},
    {...field('max_megabytes', '每个图库容量（MB）', c.max_megabytes, 'number', true), min: 1, max: 1024},
  ], save: async v => { await put('/qq/stickers/settings', {enabled: v.enabled === 'true', auto_review: v.auto_review === 'true', profile_id: v.profile_id || null, taste: v.taste, hourly_limit: Number(v.hourly_limit), max_items: Number(v.max_items), max_megabytes: Number(v.max_megabytes)}); notify('收藏偏好已保存'); await load() } })
}
async function favorite(item: Item) { await patch(`/qq/stickers/${item.id}`, {favorite: !item.favorite}); item.favorite = !item.favorite; await load() }
function edit(item: Item) { openForm({title: '整理这张图片', fields: [field('title', '图片名称', item.title, 'text', true), field('tags', '标签（逗号分隔）', item.tags.join('，'))], save: async v => { const tags = v.tags.split(/[,，]/).map(t => t.trim()).filter(Boolean); await patch(`/qq/stickers/${item.id}`, { title: v.title, tags }); item.title = v.title; item.tags = tags; notify('已保存名称与标签'); await load() }}) }
function forget(item: Item) { confirmAction('从收藏中删除？', '图片将停止展示并释放收藏容量，模型不会自动重新收藏。主人再次明确命令收藏时可以重新保存。', async () => { await remove(`/qq/stickers/${item.id}`); selected.value = null; await load(); notify('已删除这张图片') }) }
async function download(item: Item) {
  if (downloading.value) return
  downloading.value = true
  try { const response = await fetch(`/api/v2/qq/stickers/${item.id}/image?download=true`, {headers: authHeaders()}); if (!response.ok) throw Error('原图下载失败'); const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = `${item.title.replace(/[\\/:*?"<>|]/g,'_')}.${item.mime === 'image/jpeg' ? 'jpg' : item.mime.split('/')[1]}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
  finally { downloading.value = false }
}
async function retry() { const result = await post('/qq/stickers/retry'); notify(`已重新提交 ${result.queued} 项`); await load() }
</script>
<template>
  <PageHeading title="把喜欢，收进小口袋" description="好看的图片、有趣的表情，灵犀看过喜欢才留下。" eyebrow="LITTLE THINGS, WORTH KEEPING">
    <RouterLink to="/companion" class="button secondary">QQ 私聊<ArrowUpRight :size="15" /></RouterLink>
    <RouterLink to="/qq" class="button secondary">群接入<ArrowUpRight :size="15" /></RouterLink>
    <button class="button" :disabled="loading" @click="run(configure)"><Settings2 :size="16" />收藏偏好</button>
  </PageHeading>
  <section class="sticker-intro">
    <div class="sticker-intro-copy"><span class="sticker-kicker"><Sparkles :size="14" />灵犀的图片与表情收藏</span><h2>看懂你的图片，<br />收下喜欢的瞬间。</h2><p>私聊中可以直接发图、发图提问，或回复图片说「收藏这张图」。<br />喜欢的表情也能成为回复；私人收藏只在主人私聊中使用。</p><span :class="['tag', (data.qq_enabled || data.private_enabled) && data.settings.enabled ? 'green' : 'neutral']">{{ !(data.qq_enabled || data.private_enabled) ? '等待连接 QQ' : !data.settings.enabled ? '收藏尚未开启' : data.settings.auto_review ? '模型挑选 + 主人命令' : '仅主人命令' }}</span></div>
    <div class="sticker-art" aria-hidden="true"><div class="paper-sticker paper-sun"><span>☀</span><small>今天也很好</small></div><div class="paper-sticker paper-face"><div class="face-eyes"><i></i><i></i></div><div class="face-smile"></div><small>喜欢，收下。</small></div><span class="sticker-spark">✧</span><span class="sticker-heart">♡</span></div>
  </section>
  <div class="sticker-numbers"><span><Sticker :size="17" /><strong>{{ data.stats.count }}</strong> 张收藏</span><span><Heart :size="17" /><strong>{{ data.stats.favorites }}</strong> 张特别喜欢</span><span><Image :size="17" />已用 {{ bytes(data.stats.bytes) }} · 每库上限 {{ data.settings.max_megabytes }} MB</span><span v-if="data.stats.pending"><Eye :size="17" />{{ data.stats.pending }} 张待处理</span></div>
  <section class="sticker-library">
    <div class="sticker-toolbar"><div class="sticker-tabs"><button :class="{ active: !favoriteOnly }" @click="filter(false)">全部收藏</button><button :class="{ active: favoriteOnly }" @click="filter(true)"><Heart :size="14" />特别喜欢</button></div><form @submit.prevent="filter()"><Search :size="17" /><input v-model="search" placeholder="搜索名称、标签、收藏理由" aria-label="搜索图片收藏" /><button class="icon-button" aria-label="搜索收藏"><ArrowUpRight :size="16" /></button></form><button class="icon-button" aria-label="刷新收藏" :disabled="loading" @click="load"><RefreshCw :size="17" /></button></div>
    <ResourceState :loading="loading" :error="error" @retry="load">
      <EmptyState v-if="!data.items.length" :title="query || favoriteOnly ? '还没有找到这样的图片' : '第一张喜欢的图，还在路上'" :description="query || favoriteOnly ? '试试其他关键词，或看看全部收藏。' : '开启收藏后，模型会挑选喜欢的图片。主人也可以发图或回复一张图片，说「收藏这张图」。'"><button v-if="!query && !favoriteOnly" class="button secondary" @click="run(configure)">设置收藏偏好<ArrowUpRight :size="15" /></button></EmptyState>
      <div v-else class="sticker-grid"><article v-for="item in data.items" :key="item.id" class="sticker-card"><button class="sticker-picture" :aria-label="`查看 ${item.title}`" @click="selected = item"><StickerImage :id="item.id" :title="item.title" /><span class="sticker-format">{{ item.mime.split('/')[1].toUpperCase() }}</span></button><div class="sticker-card-copy"><div class="sticker-card-title"><h3>{{ item.title }}</h3><button class="icon-button" :class="{ 'is-favorite': item.favorite }" :aria-label="item.favorite ? '取消特别喜欢' : '标为特别喜欢'" @click="run(() => favorite(item))"><Heart :size="17" :fill="item.favorite ? 'currentColor' : 'none'" /></button></div><p>{{ item.reason }}</p><div class="sticker-tags"><span>{{ sourceLabel(item) }}</span><span v-for="tag in item.tags" :key="tag">{{ tag }}</span></div><div class="sticker-card-bottom"><span><Sparkles v-if="item.collected_by === 'model'" :size="12" /><MessageSquare v-else :size="12" />{{ item.collected_by === 'model' ? '灵犀喜欢' : '主人收藏' }}</span><button class="icon-button" aria-label="下载原图" :disabled="downloading" @click="run(() => download(item))"><Download :size="15" /></button></div></div></article></div>
      <div v-if="data.total > 40" class="sticker-pagination"><button class="button secondary" :disabled="page <= 1" @click="turn(-1)"><ChevronLeft :size="15" />上一页</button><span>{{ page }} / {{ Math.ceil(data.total / 40) }}</span><button class="button secondary" :disabled="page * 40 >= data.total" @click="turn(1)">下一页<ChevronRight :size="15" /></button></div>
    </ResourceState>
  </section>
  <details v-if="data.recent.length" class="sticker-activity"><summary>最近的收藏判断 <span v-if="data.stats.failed">· {{ data.stats.failed }} 项处理失败</span></summary><div v-for="(entry,index) in data.recent" :key="index"><span :class="['tag', entry.status === 'saved' ? 'green' : 'neutral']">{{ statuses[entry.status] || entry.status }}</span><p>{{ entry.reason }}</p><small>{{ date(entry.time) }}</small></div><button v-if="data.stats.failed" class="button secondary" @click="run(retry)"><RefreshCw :size="14" />重试失败项</button></details>
  <dialog ref="dialog" class="sticker-dialog" aria-labelledby="sticker-detail-title" @cancel.prevent="selected = null" @click="($event.target === dialog) && (selected = null)"><article v-if="selected"><div class="sticker-detail-top"><span class="eyebrow">A LITTLE KEEPSAKE</span><button class="icon-button" aria-label="关闭图片预览" @click="selected = null"><X /></button></div><div class="sticker-detail-picture"><StickerImage :id="selected.id" :title="selected.title" original /></div><h2 id="sticker-detail-title">{{ selected.title }}</h2><p class="sticker-detail-reason">{{ selected.reason }}</p><div class="sticker-tags"><span v-for="tag in selected.tags" :key="tag">{{ tag }}</span></div><p class="sticker-detail-meta">{{ selected.width }} × {{ selected.height }} · {{ bytes(selected.byte_size) }} · {{ sourceLabel(selected) }} · 出现 {{ selected.occurrences }} 次</p><div class="sticker-detail-actions"><button class="button secondary" @click="edit(selected)"><Pencil :size="15" />整理</button><button class="button secondary" @click="forget(selected)"><Trash2 :size="15" />删除</button><button class="button" :disabled="downloading" @click="run(async () => { if (selected) await download(selected) })"><Download :size="15" />下载原图</button></div></article></dialog>
</template>
