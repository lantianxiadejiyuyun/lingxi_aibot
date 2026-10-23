<script setup lang="ts">
import { MessagesSquare, Settings2, ArrowUpRight } from '@lucide/vue'
import { api, put } from '../lib/api'
import { openForm, notify, run } from '../lib/ui'
import { rhythmFields, rhythmValue, type Rhythm } from '../lib/qq-dialogue'
defineProps<{settings?:Rhythm}>()
const emit=defineEmits<{changed:[]}>()
async function edit(){
 const cfg=await api<Record<string,any>>('/integrations');const q=cfg.qq||{};
 openForm({title:'主人私聊的聊天节奏',description:'分条回复与延迟补充各自独立。主动开启新话题在「主动陪伴」中设置；延迟补充也遵守那里的活动、免打扰时段和忙碌日程。',fields:rhythmFields(q),save:async v=>{
  const fresh=await api<Record<string,any>>('/integrations');await put('/integrations',{...fresh,qq:{...fresh.qq,...rhythmValue(v)}});notify('私聊节奏已保存');emit('changed')
 }})
}
</script>
<template>
 <section class="panel spaced"><div class="section-heading"><div class="inline-actions"><span class="stat-icon lilac"><MessagesSquare :size="22" /></span><div><h2>聊得自然一点</h2><p>给主人分几条说，也可以在有新想法时轻轻补充一句。</p></div></div><button class="button secondary" @click="run(edit)"><Settings2 :size="15" />私聊节奏</button></div>
 <div class="qq-group-meta"><span>{{settings?.reply_style?.enabled?`一轮最多 ${settings.reply_style.max_messages} 条`:'一轮一条'}}</span><span>{{settings?.followup?.enabled?`${settings.followup.delay_minutes} 分钟后由模型决定是否继续聊`:'延迟补充关闭'}}</span></div>
 <p class="muted small">通过 NapCat 发送到主人的 QQ 私聊。各群可在自己的「聊天节奏」里单独设置。</p><RouterLink to="/companion" class="text-link">设置主人主动陪伴<ArrowUpRight :size="14" /></RouterLink>
 </section>
</template>
