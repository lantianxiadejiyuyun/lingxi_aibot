<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Moon, MessagesSquare, Sparkles, ArrowUpRight } from '@lucide/vue'
import { api, put } from '../lib/api'
import { notify } from '../lib/ui'
import { useResource } from '../lib/resource'
import ResourceState from './ResourceState.vue'
type Mode = 'quiet' | 'natural' | 'lively'
interface Preset {mode:Mode;paragraphs:number;followup_delay:number;followup_round:number;followup_daily:number;group_cooldown:number;group_hourly:number}
interface View {mode:Mode;configured:boolean;customized:boolean;revision:string;group_count:number;presets:Preset[]}
const styles = [
 {mode:'quiet' as Mode,name:'安静陪伴',icon:Moon,description:'话少一点，认真听你说。需要时再展开。',example:'今天辛苦了，先歇会儿。'},
 {mode:'natural' as Mode,name:'自然相处',icon:MessagesSquare,description:'有主见，能接梗；聊到这里，也懂得收住。',example:'这事确实够折腾的。换我也想先躺一会儿。'},
 {mode:'lively' as Mode,name:'轻松活跃',icon:Sparkles,description:'多一点兴致和打趣，仍然给彼此留空间。',example:'今日电量耗尽了？那先暂停营业，休息优先。'},
]
const {data,loading,error,load}=useResource(()=>api<View>('/interaction'),{mode:'natural',configured:false,customized:false,revision:'',group_count:0,presets:[]} as View)
const selected=ref<Mode>('natural'),busy=ref(false),saveError=ref('')
watch(data,v=>{selected.value=v.mode})
const preview=computed(()=>data.value.presets.find(p=>p.mode===selected.value))
async function save(){busy.value=true;saveError.value='';try{data.value=await put<View>('/interaction',{mode:selected.value,revision:data.value.revision});notify('相处方式已生效')}catch(e){saveError.value=e instanceof Error?e.message:'保存失败'}finally{busy.value=false}}
</script>
<template><ResourceState :loading="loading" :error="error" @retry="load">
 <section class="interaction-intro"><span class="eyebrow">A LITTLE MORE LIKE A FRIEND</span><h2>先选一种舒服的相处方式</h2><p>不必逐项调参数。语气、分段和接话节奏一起安排，基础人设由你来定。</p><span class="tag neutral">{{!data.configured?'推荐自然相处':data.customized?'已有单独调整':'当前：'+styles.find(s=>s.mode===data.mode)?.name}}</span></section>
 <div class="interaction-presets" role="group" aria-label="选择相处方式"><button v-for="s in styles" :key="s.mode" type="button" :class="['interaction-preset',{selected:selected===s.mode}]" :aria-pressed="selected===s.mode" :disabled="busy" @click="selected=s.mode"><component :is="s.icon" :size="24" /><span v-if="s.mode==='natural'" class="preset-recommended">推荐</span><strong>{{s.name}}</strong><span>{{s.description}}</span><q>{{s.example}}</q></button></div>
 <section v-if="preview" class="interaction-preview" aria-live="polite"><h3>选好后，会这样聊天</h3><div class="interaction-facts"><span>短话一段，有需要再分段<br /><b>每轮最多 {{preview.paragraphs}} 段，间隔随机 1–3 秒</b></span><span>已经开启延迟补充时<br /><b>至少等 {{preview.followup_delay}} 分钟，每轮最多 {{preview.followup_round}} 次</b></span><span>已经开启群自动接话时<br /><b>间隔至少 {{preview.group_cooldown/60}} 分钟，每小时最多 {{preview.group_hourly}} 次</b></span></div><p>语气用于所有对话；发送节奏用于 QQ / 飞书私聊和 {{data.group_count}} 个已接入群。主动聊天、补充与各项权限沿用现有开关。示例只说明语气，不会反复套用。</p><p v-if="data.customized" class="muted small">你曾单独调整过节奏。应用预设会统一这些数值；之后仍可逐群微调。</p><p v-if="saveError" role="alert" class="form-error">{{saveError}} <button class="text-link" :disabled="busy" @click="load">刷新设置</button></p><button class="button" :disabled="busy" @click="save">{{busy?'正在保存…':'应用'+styles.find(s=>s.mode===selected)?.name}}</button></section>
 <div class="interaction-links"><RouterLink to="/settings?tab=models"><strong>它是什么样的</strong><span>模型、基础人设与相处养成</span><ArrowUpRight :size="18" /></RouterLink><RouterLink to="/settings?tab=activities&panel=companion"><strong>什么时候主动找我</strong><span>主动陪伴、免打扰与发送渠道</span><ArrowUpRight :size="18" /></RouterLink><RouterLink to="/settings?tab=activities&panel=mood"><strong>心情慢慢变化</strong><span>情绪判断与各会话的状态</span><ArrowUpRight :size="18" /></RouterLink><RouterLink to="/settings?tab=activities&panel=interests"><strong>看看图片，逛逛 B 站</strong><span>收藏偏好与自主阅读</span><ArrowUpRight :size="18" /></RouterLink></div>
 <p class="muted small">需要更细的控制？<RouterLink to="/settings?tab=features">功能与权限</RouterLink> 可分别授权个人和群聊；<RouterLink to="/settings?tab=qq">QQ 与群聊</RouterLink> 可为单个群调整。</p>
</ResourceState></template>
