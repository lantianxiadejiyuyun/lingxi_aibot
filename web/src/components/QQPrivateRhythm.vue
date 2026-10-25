<script setup lang="ts">
import { MessagesSquare, Settings2, ArrowUpRight } from '@lucide/vue'
import { api, put } from '../lib/api'
import { openForm, notify, run } from '../lib/ui'
import { rhythmFields, rhythmValue, type Rhythm } from '../lib/qq-dialogue'
defineProps<{settings?:Rhythm}>()
const emit=defineEmits<{changed:[]}>()
async function edit(){
 const cfg=await api<Record<string,any>>('/integrations');const q=cfg.qq||{};
 openForm({title:'主人私聊的聊天节奏',description:'分段回复用于 QQ 与飞书私聊；QQ 延迟补充独立控制。主动开启新话题在「设置中心 → 习惯与偏好」中设置；延迟补充也遵守那里的活动、免打扰时段和忙碌日程。',fields:rhythmFields(q),save:async v=>{
  const fresh=await api<Record<string,any>>('/integrations');await put('/integrations',{...fresh,qq:{...fresh.qq,...rhythmValue(v)}});notify('私聊节奏已保存');emit('changed')
 }})
}
</script>
<template>
 <section class="panel spaced"><div class="section-heading"><div class="inline-actions"><span class="stat-icon lilac"><MessagesSquare :size="22" /></span><div><h2>聊得自然一点</h2><p>给主人分几条说，也可以在有新想法时轻轻补充一句。</p></div></div><button class="button secondary" @click="run(edit)"><Settings2 :size="15" />私聊节奏</button></div>
 <div class="qq-group-meta"><span>{{settings?.reply_style?.enabled?`一轮最多 ${settings.reply_style.max_messages} 条`:'一轮一条'}}</span><span>{{settings?.followup?.enabled?`${settings.followup.delay_minutes} 分钟后由模型决定是否继续聊`:'延迟补充关闭'}}</span></div>
 <p class="muted small">QQ 连续消息会等安静 5 秒再打包，最长等待 20 秒；QQ 和飞书分段回复的间隔随机为 1–3 秒。延迟补充通过 NapCat 发送到主人的 QQ，各群有独立设置。</p><RouterLink to="/settings?tab=activities&panel=companion" class="text-link">设置主人主动陪伴<ArrowUpRight :size="14" /></RouterLink>
 </section>
</template>
