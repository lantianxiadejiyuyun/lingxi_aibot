<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Fish, Sprout, Heart, LockKeyhole, Sparkles, Pencil, Trash2, Plus, Quote, Clock3 } from '@lucide/vue'
import { api, post, put, remove } from '../lib/api'
import { choice, confirmAction, date, field, notify, openForm, run } from '../lib/ui'
import PageHeading from '../components/PageHeading.vue'
import ResourceState from '../components/ResourceState.vue'
type Profile = { id: string; name: string; system_prompt: string }
type Trait = { id: string; kind: string; name: string; content: string; locked: boolean; source: string; evidence: { message_id: number; quote: string }[]; updated_at: number }
type Growth = { enabled: boolean; revision: number; turns: number; days: number; pending: number; last_reflected_at: number | null; last_error: string; traits: Trait[] }
const kinds: [string,string][] = [['temperament','性格细节'],['address','专属称呼'],['habit','相处习惯'],['interest','共同兴趣'],['memory','共同回忆']]
const profiles = ref<Profile[]>([]), selected = ref(''), data = ref<Growth | null>(null), loading = ref(true), busy = ref(false), error = ref('')
const profile = computed(() => profiles.value.find(p => p.id === selected.value))
const stage = computed(() => !data.value?.turns ? '故事才刚开始' : data.value.days >= 7 && data.value.turns >= 30 ? '攒下了许多日常' : '一点点熟悉起来')
let sequence = 0
async function load() {
 const seq = ++sequence; loading.value = true; error.value = ''; data.value = null
 try { if (!selected.value) return; const result = await api<Growth>(`/persona/${encodeURIComponent(selected.value)}`); if (seq === sequence) data.value = result }
 catch (e) { if (seq === sequence) error.value = e instanceof Error ? e.message : '暂时无法读取养成记录' }
 finally { if (seq === sequence) loading.value = false }
}
async function boot() { try { profiles.value = await api<Profile[]>('/profiles'); selected.value = profiles.value[0]?.id || ''; await load() } catch(e) { error.value = e instanceof Error ? e.message : '读取配置失败'; loading.value = false } }
onMounted(boot)
async function toggle() { if (!data.value || busy.value) return; const pid = selected.value; busy.value = true; try { const result = await put<Growth>(`/persona/${encodeURIComponent(pid)}`, { enabled: !data.value.enabled, revision: data.value.revision }); if (selected.value === pid) data.value = result; notify(result.enabled ? '开始记录新的相处片段' : '已暂停学习，已有成长继续保留') } finally { busy.value = false } }
function edit(t?: Trait) {
 const pid = selected.value
 openForm({ title: t ? '调整这份默契' : '写下一个相处习惯', description: '固定后，自动学习不会覆盖这条内容。解开固定后，新的明确偏好可以更新它。', fields: [
  {...choice('kind','属于哪一种',kinds,t?.kind || 'habit'), readonly: !!t},
  {...field('name','给它起个名字',t?.name || '', 'text',true),readonly:!!t}, field('content','希望怎样相处',t?.content || '', 'textarea',true), choice('locked','是否固定',[['true','固定保留'],['false','允许随相处更新']],String(t?.locked ?? true)),
 ], save: async v => { await post(`/persona/${encodeURIComponent(pid)}/traits`,{kind:t?.kind || v.kind,name:t?.name || v.name,content:v.content,locked:v.locked === 'true'}); if(selected.value === pid) await load(); notify('这份默契已记下') } })
}
function forget(t: Trait) { const pid = selected.value; confirmAction('忘记这条成长？','同名条目不会被自动学习重新添加；之后仍可手动重新记下。',async()=>{await remove(`/persona/${encodeURIComponent(pid)}/traits/${t.id}`);if(selected.value === pid)await load()}) }
</script>
<template>
 <PageHeading title="一起，慢慢长成喜欢的样子" description="底色是初见时的它，默契来自后来的每一天。" eyebrow="OUR LITTLE STORY" />
 <ResourceState :loading="loading" :error="error" @retry="profiles.length ? load() : boot()">
  <section v-if="!profiles.length" class="panel persona-empty"><Fish :size="40"/><h2>先给故事一个主角</h2><p>添加模型与核心人设后，就可以开始养成。</p><RouterLink class="button" to="/settings?tab=models">设置人设</RouterLink></section>
  <template v-else>
   <section class="persona-hero">
    <div class="persona-fish" aria-hidden="true"><Fish :size="66" :stroke-width="1.25"/><span>慢慢来，也很好</span></div>
    <div class="persona-hero-copy"><span class="persona-eyebrow"><Sprout :size="14"/> {{ stage }}</span><h2>那些只属于我们的习惯</h2><p>也许是一个新称呼，一句懂你的玩笑，或是聊起某件小事时，自然而然想起你。</p>
     <label class="persona-select">当前人设 <select v-model="selected" :disabled="busy" @change="load"><option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
    </div>
    <div class="persona-stat"><strong>{{ data?.traits.length || 0 }}</strong><span>份相处印记</span><small>{{ data?.turns || 0 }} 次对话 · {{ data?.days || 0 }} 个有聊天的日子</small></div>
   </section>
   <div v-if="data" class="persona-layout">
    <aside class="persona-sidebar">
     <section class="panel persona-panel"><div class="persona-section-title"><Heart :size="18"/><h3>随相处慢慢成长</h3></div><p>从开启后的网页聊天、主人 QQ 私聊与绑定的 AstrBot 私聊中学习。</p><button class="button persona-toggle" :class="{secondary:data.enabled}" :disabled="busy" :aria-pressed="data.enabled" @click="run(toggle)">{{ busy ? '正在保存…' : data.enabled ? '暂停自动学习' : '开启自动学习' }}</button><small>每积累 6 次成功对话再整理，两次整理至少间隔 2 小时。没有值得记住的变化，也可以什么都不添。</small><p class="persona-status"><Clock3 :size="14"/>{{ data.last_reflected_at ? '上次整理 ' + date(data.last_reflected_at) : '还没有整理记录' }}</p><small v-if="data.enabled">当前有 {{ data.pending }} 个新片段等待整理。</small><p v-if="data.last_error" class="persona-error" role="status">{{ data.last_error }}</p></section>
     <section class="panel persona-panel"><div class="persona-section-title"><LockKeyhole :size="18"/><h3>初见时的底色</h3></div><p>身份、世界观和核心性格由你设定。养成只补充日常相处，不自动改写这部分。</p><details><summary>看看核心人设</summary><pre>{{ profile?.system_prompt || '尚未填写专属人设' }}</pre></details><RouterLink class="persona-text-link" to="/settings?tab=models">编辑核心人设 →</RouterLink></section>
     <p class="persona-note">成长按账号和人设分别保存在主库，仅用于私聊与主动陪伴。群里的话题留在各自的群里。</p>
    </aside>
    <section class="persona-stories"><div class="persona-stories-heading"><div><span class="persona-eyebrow">LITTLE THINGS THAT STAY</span><h2>长出来的小默契</h2></div><button class="button secondary" @click="edit()"><Plus :size="16"/>记下一笔</button></div>
     <div v-if="!data.traits.length" class="persona-empty panel"><Sprout :size="36"/><h3>现在的空白，也是故事的一部分</h3><p>试着告诉它“以后叫我什么”，或“我喜欢你怎样和我聊天”。开启养成后，有依据的变化会慢慢出现在这里。</p><span>甜甜的底色 · 一点傲娇 · 一点小狡黠</span></div>
     <div v-else class="persona-traits"><article v-for="t in data.traits" :key="t.id" class="persona-trait panel" :class="'trait-' + t.kind"><div class="persona-trait-meta"><span class="tag lilac">{{ kinds.find(k=>k[0]===t.kind)?.[1] }}</span><span v-if="t.locked" class="persona-lock"><LockKeyhole :size="12"/>已固定</span><span v-else class="persona-lock"><Sparkles :size="12"/>可成长</span></div><h3>{{ t.name }}</h3><p>{{ t.content }}</p><details v-if="t.evidence.length"><summary><Quote :size="13"/>从这些话里记住的</summary><blockquote v-for="e in t.evidence" :key="e.message_id">{{ e.quote }}</blockquote></details><div class="persona-trait-footer"><small>{{ t.source === 'manual' ? '你亲手写下' : '从相处中学会' }} · {{ date(t.updated_at,true) }}</small><div><button class="icon-button" :aria-label="'编辑' + t.name" @click="edit(t)"><Pencil :size="15"/></button><button class="icon-button danger" :aria-label="'忘记' + t.name" @click="forget(t)"><Trash2 :size="15"/></button></div></div></article></div>
    </section>
   </div>
  </template>
 </ResourceState>
</template>
<style scoped>
.persona-hero{display:flex;gap:28px;align-items:center;padding:32px;border:1px solid #ddd1e9;border-radius:22px;background:linear-gradient(120deg,#f0dfea,#f7f6f5 65%,#ddd1e9);margin-bottom:24px}.persona-fish{display:grid;justify-items:center;gap:12px;min-width:128px;color:#82647b}.persona-fish span{font-size:11px;letter-spacing:2px}.persona-hero-copy{flex:1}.persona-hero h2{font-size:25px;margin:10px 0}.persona-hero p{max-width:510px;line-height:1.9;color:#706879;margin-bottom:18px}.persona-eyebrow{display:flex;align-items:center;gap:7px;color:#836b82;font-size:11px;letter-spacing:1.5px}.persona-select{display:flex;align-items:center;gap:12px;font-size:12px;color:#716778}.persona-select select{max-width:260px;padding:8px 30px 8px 12px;border:1px solid #ddd1e9;border-radius:10px;background:#f7f6f5;color:#554a60}.persona-stat{display:grid;gap:8px;text-align:center;min-width:160px}.persona-stat strong{font-size:48px;font-weight:500;color:#806987}.persona-stat span{font-size:13px}.persona-stat small{font-size:11px;color:#786d80}.persona-layout{display:grid;grid-template-columns:300px minmax(0,1fr);gap:24px;align-items:start}.persona-sidebar{display:grid;gap:18px}.persona-panel{padding:24px}.persona-section-title{display:flex;gap:9px;align-items:center;color:#7e6487}.persona-section-title h3{font-size:15px;margin:0}.persona-panel p,.persona-panel small{font-size:12px;color:#766d7c;line-height:1.9}.persona-toggle{width:100%;margin:10px 0 14px}.persona-status{display:flex;align-items:center;gap:6px;margin-bottom:0}.persona-panel details{font-size:12px;margin:16px 0}.persona-panel summary{cursor:pointer;color:#7c6685}.persona-panel pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto;font:inherit;line-height:1.8;background:#f7f6f5;padding:14px;border-radius:10px}.persona-text-link{font-size:12px;color:#7e6487}.persona-note{padding:0 9px;font-size:11px;line-height:1.9;color:#8b8190}.persona-stories-heading{display:flex;gap:15px;justify-content:space-between;align-items:center;margin:4px 0 22px}.persona-stories-heading h2{font-size:19px;margin:9px 0 0}.persona-traits{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.persona-trait{padding:22px;border-top:3px solid #ddd1e9;overflow-wrap:anywhere}.trait-address{border-top-color:#eabcdd}.trait-interest{border-top-color:#a4b5d5}.trait-memory{border-top-color:#e4c9dc}.persona-trait-meta,.persona-trait-footer{display:flex;justify-content:space-between;align-items:center;gap:8px}.persona-lock{display:flex;gap:4px;align-items:center;color:#887b8d;font-size:10px}.persona-trait h3{font-size:16px;margin:17px 0 10px}.persona-trait p{font-size:13px;line-height:1.9;white-space:pre-wrap;color:#706575}.persona-trait details{margin:18px 0;color:#8b7f90;font-size:11px}.persona-trait summary{display:flex;gap:5px;align-items:center;cursor:pointer}.persona-trait blockquote{margin:12px 0 0;border-left:2px solid #ddd1e9;padding-left:10px;line-height:1.8}.persona-trait-footer{border-top:1px solid #f0dfea;padding-top:12px;margin-top:18px}.persona-trait-footer small{font-size:10px;color:#978c9c}.persona-trait-footer>div{display:flex}.persona-empty{display:grid;justify-items:center;text-align:center;padding:48px 28px;color:#947c9b}.persona-empty h3{font-size:17px;color:#6e6078;margin-top:20px}.persona-empty p{max-width:410px;font-size:13px;line-height:2;color:#8b8091}.persona-empty span{font-size:11px;color:#a38aa5;margin-top:14px}.persona-panel .persona-error{color:#986371}@media(max-width:1100px){.persona-layout{grid-template-columns:260px minmax(0,1fr)}.persona-traits{grid-template-columns:1fr}.persona-stat{display:none}}@media(max-width:720px){.persona-hero{padding:24px;gap:15px}.persona-fish{min-width:60px}.persona-fish svg{width:52px}.persona-fish span{display:none}.persona-hero h2{font-size:21px}.persona-layout{grid-template-columns:1fr}.persona-stories{grid-row:1}.persona-stories-heading{align-items:flex-start}.persona-stories-heading .button{font-size:12px;white-space:nowrap}.persona-select{flex-wrap:wrap}}
</style>
