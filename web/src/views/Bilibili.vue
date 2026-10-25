<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { BookOpen, TvMinimalPlay, Sparkles, ArrowUpRight, Play, Settings2, RefreshCw, Heart, Search, Clock3, X, ChevronLeft, ChevronRight, Download, Trash2, ScanLine, Pause } from '@lucide/vue'
import QRCode from 'qrcode'
import { api, patch, post, put, remove, requestId } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, run } from '../lib/ui'
import { useResource } from '../lib/resource'
import type { Profile } from '../types'
import PageHeading from '../components/PageHeading.vue'
import EmptyState from '../components/EmptyState.vue'
import ResourceState from '../components/ResourceState.vue'
import '../styles/bilibili.css'

interface Config { enabled: boolean; autonomous: boolean; keywords: string[]; daily_at: string; daily_limit: number; per_run: number; recent_days: number; profile_id: string | null; taste: string }
interface Analysis { summary: string; thought: string; worth_reading: boolean; tags: string[]; points: { segment: number; text: string }[] }
interface Entry { id: string; bvid: string; page: number; url: string; title: string; author: string; duration: number; status: string; reason: string; source: string; language: string; analysis: Partial<Analysis>; favorite: boolean; updated_at: number }
interface ReadingRun { id: string; job_id: string; status: string; progress: string; report: string; created_at: number }
interface View { config: Config; account: { connected: boolean; name: string }; items: Entry[]; total: number; page: number; timezone: string; next_run_at: number; stats: { total: number; completed: number; favorites: number; today: number }; runs: ReadingRun[] }
interface Detail { segments: { start: number; end: number; text: string }[]; analysis: Partial<Analysis> }
const defaults: Config = { enabled: false, autonomous: false, keywords: [], daily_at: '19:00', daily_limit: 6, per_run: 3, recent_days: 7, profile_id: null, taste: '' }
const video = ref(''), submitting = ref(false), submitError = ref(''), search = ref(''), query = ref(''), status = ref(''), favoriteOnly = ref(false), page = ref(1), tab = ref('library')
const initial: View = { config: defaults, account: { connected: false, name: '' }, items: [], total: 0, page: 1, timezone: 'Asia/Shanghai', next_run_at: 0, stats: { total: 0, completed: 0, favorites: 0, today: 0 }, runs: [] }
const { data, loading, error, load } = useResource(() => api<View>(`/bilibili?q=${encodeURIComponent(query.value)}&status=${status.value}&favorite=${favoriteOnly.value}&page=${page.value}`), initial)
const active = computed(() => data.value.runs.find(r => ['queued', 'running'].includes(r.status)))
const statuses: Record<string, string> = { completed: '已读字幕', reading: '正在阅读', queued: '等待开始', running: '正在阅读', skipped: '没有字幕', failed: '未完成', partial: '部分完成', interrupted: '已中断' }
const selected = ref<Entry | null>(null), detail = ref<Detail | null>(null), detailError = ref(''), detailLoading = ref(false), detailDialog = ref<HTMLDialogElement>(), report = ref<ReadingRun | null>(null), reportDialog = ref<HTMLDialogElement>()
const loginDialog = ref<HTMLDialogElement>(), qr = ref(''), qrState = ref(''), qrError = ref(''), qrBusy = ref(false)
let poll: ReturnType<typeof setInterval>, loginTimer: ReturnType<typeof setTimeout> | undefined, loginEpoch = 0, detailEpoch = 0
onMounted(() => { poll = setInterval(() => { if (active.value && !loading.value) void load() }, 8000) })
onBeforeUnmount(() => { clearInterval(poll); stopLogin(); detailEpoch++ })
function filter() { page.value = 1; query.value = search.value; void load() }
function turn(delta: number) { page.value += delta; void load() }
function time(seconds: number) { return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` }
async function submit(value = video.value, retry = false) {
  if (submitting.value) return
  submitting.value = true; submitError.value = ''
  try { await post('/bilibili/run', { video: value.trim(), retry, request_id: requestId() }); video.value = ''; notify('已加入阅读队列'); await load() }
  catch (e) { submitError.value = e instanceof Error ? e.message : '提交失败' }
  finally { submitting.value = false }
}
async function openEntry(entry: Entry) {
  const epoch = ++detailEpoch; selected.value = entry; detail.value = null; detailError.value = ''; detailLoading.value = true
  await nextTick(); detailDialog.value?.showModal()
  try { const result = await api<Detail>(`/bilibili/entries/${entry.id}`); if (epoch === detailEpoch) detail.value = result }
  catch (e) { if (epoch === detailEpoch) detailError.value = e instanceof Error ? e.message : '加载失败' }
  finally { if (epoch === detailEpoch) detailLoading.value = false }
}
function closeEntry() { detailEpoch++; selected.value = null; detailDialog.value?.close() }
async function favorite(entry: Entry) { await patch(`/bilibili/entries/${entry.id}`, { favorite: !entry.favorite }); entry.favorite = !entry.favorite; await load() }
function destroy(entry: Entry) { confirmAction('删除这份阅读记录？', '将删除保存的字幕和心得；之前生成的阅读小记仍会保留。', async () => { await remove(`/bilibili/entries/${entry.id}`); closeEntry(); await load(); notify('已删除阅读记录') }) }
async function openReport(r: ReadingRun) { report.value = r; await nextTick(); reportDialog.value?.showModal() }
function downloadReport() {
  if (!report.value) return
  const url = URL.createObjectURL(new Blob([report.value.report], { type: 'text/markdown;charset=utf-8' })), a = document.createElement('a')
  a.href = url; a.download = `灵犀-B站阅读-${new Date(report.value.created_at * 1000).toISOString().slice(0, 10)}.md`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function cancelReading() { if (active.value) { await post(`/jobs/${active.value.job_id}/cancel`); notify('已请求停止阅读'); await load() } }
function stopLogin() { loginEpoch++; if (loginTimer) clearTimeout(loginTimer); qrBusy.value = false }
function closeLogin() { stopLogin(); loginDialog.value?.close() }
async function startLogin() {
  stopLogin(); const epoch = loginEpoch; qrBusy.value = true; qr.value = ''; qrState.value = '正在生成二维码'; qrError.value = ''
  await nextTick(); loginDialog.value?.showModal()
  try {
    const result = await post<{ id: string; url: string; expires_at: number }>('/bilibili/login')
    if (epoch !== loginEpoch) return
    qr.value = await QRCode.toDataURL(result.url, { width: 240, margin: 2, color: { dark: '#403547', light: '#ffffff' } }); qrState.value = '用哔哩哔哩 App 扫码'
    const tick = async () => {
      if (epoch !== loginEpoch) return
      if (Date.now() >= result.expires_at * 1000) { qrState.value = '二维码已过期，请重新生成'; qr.value = ''; return }
      try {
        const res = await post<{ state: string }>(`/bilibili/login/${result.id}`)
        if (epoch !== loginEpoch) return
        if (res.state === 'ok') { closeLogin(); notify('B 站账号已连接'); await load(); return }
        if (res.state === 'expired') { qrState.value = '二维码已过期，请重新生成'; qr.value = ''; return }
        qrState.value = res.state === 'scanned' ? '已扫码，请在手机上确认' : '用哔哩哔哩 App 扫码'
        loginTimer = setTimeout(tick, 4000)
      } catch (e) { if (epoch === loginEpoch) { qrError.value = e instanceof Error ? e.message : '登录检查失败'; qr.value = '' } }
    }
    loginTimer = setTimeout(tick, 4000)
  } catch (e) { if (epoch === loginEpoch) qrError.value = e instanceof Error ? e.message : '生成失败' }
  finally { if (epoch === loginEpoch) qrBusy.value = false }
}
function disconnect() { confirmAction('断开 B 站账号？', '会清除本机保存的登录凭据，并取消尚未完成的阅读。阅读记录会保留。', async () => { await remove('/bilibili/login'); await load(); notify('B 站账号已断开') }) }
</script>
<template>
  <PageHeading title="看见新鲜，也留下想法" description="从好奇心出发，自己挑选想读的视频，带着新的理解回来。" eyebrow="A QUIET CORNER FOR CURIOSITY">
    <RouterLink class="button secondary" to="/settings?tab=activities&panel=interests"><Settings2 :size="16" />阅读设置</RouterLink>
  </PageHeading>
  <section class="bili-hero">
    <div class="bili-hero-copy"><span class="bili-eyebrow"><TvMinimalPlay :size="16" />灵犀的 B 站书房</span><h2>好内容，<br />值得慢慢读。</h2><p>{{data.config.autonomous?'想看什么，让灵犀自己决定。':'从兴趣出发，读原始字幕，记下感想。'}}<br />{{data.config.autonomous?'先挑话题，再挑视频；也可以今天不看。':'下一次聊天，也许就有了新的话题。'}}</p><div class="bili-keywords"><span v-for="kw in data.config.keywords.slice(0, 4)" :key="kw">{{ kw }}</span><span v-if="!data.config.keywords.length">好奇心，从一个视频开始</span></div></div>
    <div class="bili-art" aria-hidden="true"><span class="bili-spark">✧</span><div class="bili-paper back"><i></i><i></i><i></i></div><div class="bili-paper front"><span class="bili-paper-label">TODAY'S DISCOVERY</span><div class="bili-tv"><i></i><i></i><Play :size="28" /></div><span>读到喜欢的，记下来。</span><div class="bili-paper-lines"><i></i><i></i></div><Heart :size="22" /></div><span class="bili-orbit">○</span></div>
  </section>
  <div class="bili-overview">
    <div><BookOpen :size="19" /><span><strong>{{ data.stats.completed }}</strong> 份阅读心得</span></div><div><Heart :size="18" /><span><strong>{{ data.stats.favorites }}</strong> 份特别喜欢</span></div><div><Clock3 :size="18" /><span>今日尝试 <strong>{{ data.stats.today }}</strong> / {{ data.config.daily_limit }}</span></div>
    <span class="tag neutral">{{ data.config.enabled ? `每天 ${data.config.daily_at} · ${data.timezone}` : '按你的节奏，手动阅读' }}</span>
  </div>
  <div class="bili-start-layout">
    <section class="panel bili-submit"><h3>把这条视频交给灵犀</h3><p>粘贴完整 B 站链接或 BV 号。分 P 视频只读链接中的那一 P。</p><form @submit.prevent="submit()"><label class="sr-only" for="bili-video">B 站视频链接或 BV 号</label><input id="bili-video" v-model="video" placeholder="https://www.bilibili.com/video/BV…" maxlength="500" :disabled="submitting" /><button class="button" :disabled="submitting || !!active || !video.trim()"><BookOpen :size="16" />{{ submitting ? '提交中…' : '读这条' }}</button></form><div class="bili-discover"><button class="text-link" :disabled="submitting || !!active" @click="submit('')"><Sparkles :size="15" />{{data.config.autonomous?'让它自己逛逛':'按兴趣发现几条'}}</button><small>{{data.config.autonomous?'会调用模型选题、选片和读字幕，选择理由留在阅读小记。':'会调用阅读模型；无字幕时跳过。'}}</small></div><p v-if="submitError" role="alert" class="form-error">{{ submitError }}</p></section>
    <section class="panel bili-account"><div class="section-heading"><h3>B 站账号</h3><span :class="['tag', data.account.connected ? 'green' : 'neutral']">{{ data.account.connected ? '已连接' : '未连接' }}</span></div><p>{{ data.account.connected ? data.account.name : '部分视频的字幕需要登录后才能获取。' }}</p><small>登录凭据加密保存在你的服务中，只用于获取内容。</small><div><button v-if="!data.account.connected" class="button secondary" @click="startLogin"><ScanLine :size="16" />扫码连接</button><template v-else><button class="text-link" @click="startLogin">重新登录</button><button class="text-link" @click="disconnect">断开连接</button></template></div></section>
  </div>
  <section v-if="active" class="bili-progress" role="status"><span class="bili-reading-dot"></span><div><strong>{{ active.progress }}</strong><small>可以离开页面，阅读会在后台继续。</small></div><button class="button secondary" @click="run(cancelReading)"><Pause :size="14" />停止</button></section>
  <section class="bili-library">
    <div class="bili-toolbar"><div class="bili-tabs"><button :class="{ active: tab === 'library' }" @click="tab = 'library'">阅读记录 <span>{{ data.stats.total }}</span></button><button :class="{ active: tab === 'reports' }" @click="tab = 'reports'">阅读小记</button></div><button class="icon-button" aria-label="刷新 B 站书房" :disabled="loading" @click="load"><RefreshCw :size="17" /></button></div>
    <form v-if="tab === 'library'" class="bili-filter" @submit.prevent="filter"><div><Search :size="16" /><input v-model="search" aria-label="搜索阅读记录" placeholder="搜索标题、UP 主、标签或心得" /><button class="icon-button" aria-label="搜索"><ArrowUpRight :size="16" /></button></div><select v-model="status" aria-label="筛选阅读状态" @change="filter"><option value="">全部状态</option><option value="completed">已读字幕</option><option value="skipped">没有字幕</option><option value="failed">未完成</option></select><label><input v-model="favoriteOnly" type="checkbox" @change="filter" /><Heart :size="14" />特别喜欢</label></form>
    <ResourceState :loading="loading" :error="error" @retry="load">
      <template v-if="tab === 'library'">
        <EmptyState v-if="!data.items.length" :title="query || status || favoriteOnly ? '还没找到这样的阅读记录' : '第一份心得，等一个好奇心'" description="从一条喜欢的视频开始，或设置兴趣，让灵犀去发现。没有取得字幕的视频会说明原因。" />
        <div class="bili-grid"><article v-for="(entry, index) in data.items" :key="entry.id" class="bili-card"><button class="bili-card-banner" :class="`tone-${index % 3}`" @click="openEntry(entry)"><span class="bili-card-number">{{ String((page - 1) * 18 + index + 1).padStart(2, '0') }}</span><TvMinimalPlay :size="36" /><span class="bili-card-duration">{{ entry.duration ? time(entry.duration) : '等待内容' }} · P{{ entry.page }}</span><span :class="['tag', entry.status === 'completed' ? 'green' : 'neutral']">{{ statuses[entry.status] || entry.status }}</span></button><div class="bili-card-body"><button class="bili-title" @click="openEntry(entry)">{{ entry.title }}</button><small>{{ entry.author || entry.bvid }}</small><p>{{ entry.status === 'completed' ? entry.analysis.summary : entry.reason || '正在获取字幕和整理心得…' }}</p><div class="bili-tags"><span v-for="tag in entry.analysis.tags || []" :key="tag">{{ tag }}</span></div><footer><button class="text-link" @click="openEntry(entry)">{{ entry.status === 'completed' ? '翻开这份心得' : '查看详情' }}<ArrowUpRight :size="14" /></button><button :class="['icon-button', { 'bili-liked': entry.favorite }]" :aria-label="entry.favorite ? '取消特别喜欢' : '特别喜欢'" @click="run(() => favorite(entry))"><Heart :size="17" :fill="entry.favorite ? 'currentColor' : 'none'" /></button></footer></div></article></div>
        <div v-if="data.total > 18" class="pagination"><button class="button secondary" :disabled="page <= 1" @click="turn(-1)"><ChevronLeft :size="16" />上一页</button><span>{{ page }} / {{ Math.ceil(data.total / 18) }}</span><button class="button secondary" :disabled="page * 18 >= data.total" @click="turn(1)">下一页<ChevronRight :size="16" /></button></div>
      </template>
      <template v-else><EmptyState v-if="!data.runs.length" title="小记还没有落笔" description="每轮阅读都会留下结果：读到了什么，哪些没有读成，都记在这里。" /><article v-for="r in data.runs" :key="r.id" class="bili-run"><div class="bili-run-icon"><BookOpen :size="20" /></div><div class="grow"><h3>{{ date(r.created_at) }}</h3><p>{{ r.progress }}</p><span class="tag neutral">{{ r.status === 'completed' ? '本轮结束' : statuses[r.status] || r.status }}</span></div><button v-if="r.report" class="button secondary" @click="openReport(r)">打开小记<ArrowUpRight :size="14" /></button></article></template>
    </ResourceState>
  </section>
  <div class="bili-footnote"><BookOpen :size="17" /><p>这里的「已读」指读完获取到的字幕，不代表观看画面或收听音频。心得保存在你的主库。开启 <RouterLink to="/companion">主动陪伴</RouterLink> 的 B 站分享后，灵犀可以把值得聊的内容带给你。</p></div>

  <dialog ref="detailDialog" class="bili-dialog" aria-labelledby="bili-detail-title" @cancel.prevent="closeEntry">
    <template v-if="selected"><div class="bili-dialog-top"><span class="tag neutral">{{ statuses[selected.status] }} · {{ selected.language || '暂无字幕' }}</span><button class="icon-button" aria-label="关闭阅读详情" @click="closeEntry"><X :size="19" /></button></div><h2 id="bili-detail-title">{{ selected.title }}</h2><p class="bili-detail-meta">{{ selected.author }} · P{{ selected.page }} · {{ date(selected.updated_at) }}</p><a class="text-link" :href="selected.url" target="_blank" rel="noopener noreferrer">前往原视频<ArrowUpRight :size="15" /></a><p v-if="selected.reason" class="bili-detail-notice">{{ selected.reason }}</p><p v-if="detailLoading">正在打开阅读笔记…</p><p v-if="detailError" role="alert" class="form-error">{{ detailError }}</p><template v-if="detail && selected.status === 'completed'"><section class="bili-detail-section"><h3>视频里说了什么</h3><p>{{ detail.analysis.summary }}</p></section><section class="bili-thought"><span><Sparkles :size="17" />灵犀的想法</span><p>{{ detail.analysis.thought }}</p></section><section class="bili-detail-section"><h3>值得记住的片段</h3><a v-for="(point, i) in detail.analysis.points || []" :key="i" class="bili-point" :href="`${selected.url}&t=${Math.floor(detail.segments[point.segment]?.start || 0)}`" target="_blank" rel="noopener noreferrer"><time>{{ time(detail.segments[point.segment]?.start || 0) }}</time><span>{{ point.text }}</span><ArrowUpRight :size="14" /></a></section><details class="bili-transcript"><summary>查看原始字幕 · {{ detail.segments.length }} 段</summary><p v-for="(s, i) in detail.segments" :key="i"><time>{{ time(s.start) }}</time>{{ s.text }}</p></details></template><footer class="bili-detail-actions"><button v-if="selected.status !== 'reading'" class="icon-button danger" aria-label="删除阅读记录" @click="destroy(selected)"><Trash2 :size="17" /></button><button v-if="selected.status !== 'reading'" class="button secondary" :disabled="submitting || !!active" @click="submit(selected.url, true)">重新阅读</button><button class="button" @click="run(async () => { if (selected) await favorite(selected) })"><Heart :size="15" />{{ selected.favorite ? '取消特别喜欢' : '特别喜欢' }}</button></footer></template>
  </dialog>
  <dialog ref="reportDialog" class="bili-dialog" aria-label="B 站阅读小记"><div class="bili-dialog-top"><h2>阅读小记</h2><button class="icon-button" aria-label="关闭小记" @click="reportDialog?.close()"><X :size="19" /></button></div><pre class="bili-report">{{ report?.report }}</pre><button class="button secondary" @click="downloadReport"><Download :size="16" />下载 Markdown</button></dialog>
  <dialog ref="loginDialog" class="bili-dialog bili-login" aria-labelledby="bili-login-title" @cancel.prevent="closeLogin"><div class="bili-dialog-top"><h2 id="bili-login-title">连接你的 B 站</h2><button class="icon-button" aria-label="关闭扫码登录" @click="closeLogin"><X :size="19" /></button></div><p>打开哔哩哔哩 App，扫描并确认登录。</p><img v-if="qr" :src="qr" alt="B 站登录二维码" width="240" height="240" /><div v-else class="bili-qr-placeholder"><ScanLine :size="52" /></div><strong aria-live="polite">{{ qrState }}</strong><p v-if="qrError" role="alert" class="form-error">{{ qrError }}</p><button class="button secondary" :disabled="qrBusy" @click="startLogin"><RefreshCw :size="15" />{{ qrBusy ? '生成中…' : '重新生成' }}</button><small>凭据仅保存在当前灵犀账号下，模型无法读取。</small></dialog>
</template>
