<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, put } from '../lib/api'
import type { Profile } from '../types'
import { notify } from '../lib/ui'
const props=defineProps<{profiles:Profile[]}>()
const selected=ref(''),config=ref<{enabled:boolean;revision:number}|null>(null),error=ref(''),busy=ref(false)
let sequence=0
watch(()=>props.profiles,profiles=>{if(!profiles.some(p=>p.id===selected.value))selected.value=profiles[0]?.id||''},{immediate:true})
watch(selected,async id=>{const seq=++sequence;config.value=null;error.value='';if(!id)return;try{const v=await api<{enabled:boolean;revision:number}>(`/persona/${encodeURIComponent(id)}`);if(seq===sequence)config.value=v}catch(e){if(seq===sequence)error.value=e instanceof Error?e.message:'读取失败'}},{immediate:true})
async function toggle(event:Event){if(!config.value)return;(event.target as HTMLInputElement).checked=config.value.enabled;const id=selected.value;busy.value=true;error.value='';try{const v=await put<{enabled:boolean;revision:number}>(`/persona/${encodeURIComponent(id)}`,{enabled:!config.value.enabled,revision:config.value.revision});if(id===selected.value)config.value=v;notify('养成设置已保存')}catch(e){config.value=config.value?{...config.value}:null;error.value=e instanceof Error?e.message:'保存失败'}finally{busy.value=false}}
</script>
<template><section v-if="profiles.length" class="panel spaced"><div class="section-heading"><div><h2>基础人设与相处养成</h2><p>基础提示词在上方模型中编辑；每个模型可以独立开启养成。</p></div></div><div class="config-form-grid"><label>选择模型<select v-model="selected" :disabled="busy"><option v-for="p in profiles" :key="p.id" :value="p.id">{{p.name}}</option></select></label><label v-if="config" class="config-check"><input type="checkbox" :checked="config.enabled" :disabled="busy" @change="toggle" />从新的相处中学习习惯</label></div><p v-if="error" role="alert" class="form-error">{{error}}</p><div class="config-form-actions"><RouterLink to="/persona" class="text-link">查看与整理已形成的人设</RouterLink><RouterLink to="/settings?tab=features" class="text-link">查看人设养成权限</RouterLink></div></section></template>
