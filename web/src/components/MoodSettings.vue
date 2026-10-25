<script setup lang="ts">
import { ref, watch } from 'vue'
import { Heart } from '@lucide/vue'
import { api, put } from '../lib/api'
import { useResource } from '../lib/resource'
import { notify } from '../lib/ui'
import type { Profile } from '../types'
import ResourceState from './ResourceState.vue'
import MoodPanel from './MoodPanel.vue'
defineProps<{profiles:Profile[]}>()
type Config={enabled:boolean;profile_id:string|null;baseline:number;half_life_hours:number;emotion_half_life_hours:number;confidence_threshold:number;min_interval_secs:number;daily_judgments:number;daily_injections:number}
type Data={config:Config;scopes:{id:string;name:string;kind:string}[];allowed_private:boolean;allowed_group:boolean}
const {data,loading,error,load}=useResource(()=>api<Data>('/mood'),{config:null,scopes:[]} as unknown as Data)
const draft=ref<Config|null>(null),selected=ref(''),busy=ref(false),saveError=ref('')
watch(data,v=>{draft.value={...v.config};if(!v.scopes.some(s=>s.id===selected.value))selected.value=v.scopes[0]?.id||''})
async function save(){if(!draft.value)return;busy.value=true;saveError.value='';try{await put('/mood',{...draft.value,profile_id:draft.value.profile_id||null});await load();notify('心情设置已保存')}catch(e){saveError.value=e instanceof Error?e.message:'保存失败'}finally{busy.value=false}}
</script>
<template><section class="panel spaced"><div class="section-heading"><div><h2>心情有起伏，也有余温</h2><p>基础人设之外，留下轻微、短暂的情绪变化。每个个人会话与 QQ 群独立记录。</p></div><Heart :size="23" /></div><ResourceState :loading="loading" :error="error" @retry="load"><form v-if="draft" class="config-form" @submit.prevent="save"><div class="config-form-grid"><label class="config-check"><input v-model="draft.enabled" type="checkbox" />允许记录与使用心情</label><label>心情判断模型<select v-model="draft.profile_id"><option :value="null">沿用当前会话的模型</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{p.name}}</option></select><small>复用预设接口与预算。回复完成后异步判断，会额外调用一次模型。</small></label></div><details class="config-details"><summary>高级设置 · 心情衰减与判断预算</summary><div class="config-form-grid"><label>平稳时的心情<input v-model.number="draft.baseline" type="number" min="30" max="80" required /></label><label>整体心情淡化一半（小时）<input v-model.number="draft.half_life_hours" type="number" min="1" max="168" step="0.5" required /></label><label>具体情绪淡化一半（小时）<input v-model.number="draft.emotion_half_life_hours" type="number" min="0.25" max="48" step="0.25" required /></label><label>最低判断置信度<input v-model.number="draft.confidence_threshold" type="number" min="0.5" max="1" step="0.05" required /></label><label>同会话判断至少间隔（秒）<input v-model.number="draft.min_interval_secs" type="number" min="0" max="3600" required /></label><label>每个会话 24 小时最多判断<input v-model.number="draft.daily_judgments" type="number" min="0" max="500" required /></label><label>24 小时最多影响语气次数<input v-model.number="draft.daily_injections" type="number" min="0" max="24" required /></label></div></details><p class="muted small">依据不足时不改变心情；普通交流可以没有变化。仅明显变化会轻微影响下一轮语气，同一次变化最多使用一次。</p><p class="muted small">个人权限：{{data.allowed_private?'已允许':'未开启'}} · 群聊权限：{{data.allowed_group?'已允许':'未开启'}}。在<RouterLink to="/settings?tab=features">功能与权限</RouterLink>分别开启。</p><p v-if="saveError" role="alert" class="form-error">{{saveError}}</p><button class="button" :disabled="busy">{{busy?'保存中…':'保存心情设置'}}</button></form><label v-if="data.scopes.length" class="scope-picker">查看哪个会话<select v-model="selected"><option v-for="s in data.scopes" :key="s.id" :value="s.id">{{s.kind==='group'?'群聊':'个人'}} · {{s.name}}</option></select></label><MoodPanel v-if="selected" :key="selected" :scope="selected" /></ResourceState></section></template>
<style scoped>.scope-picker{display:grid;gap:8px;margin-top:24px;font-size:13px}.scope-picker select{max-width:480px}</style>
