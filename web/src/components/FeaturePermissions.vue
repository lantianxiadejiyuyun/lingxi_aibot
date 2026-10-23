<script setup lang="ts">
import { computed, ref } from 'vue'
import { ShieldCheck, MessageCircle, UsersRound } from '@lucide/vue'
import { api, put } from '../lib/api'
import { useResource } from '../lib/resource'
import { notify } from '../lib/ui'
import ResourceState from './ResourceState.vue'
type Grant = { enabled: boolean; private: boolean; group: boolean }
type Permissions = { enabled: boolean; features: Record<string, Grant> }
const items = [
 {key:'chat',name:'对话与回复',detail:'接收消息并生成回复',group:true},
 {key:'vision',name:'图片与 GIF 识别',detail:'GIF 最多看 10 帧；群内用于收藏判断',group:true},
 {key:'sticker_collection',name:'图片收藏',detail:'模型喜欢才收下，或按主人命令收藏',group:true},
 {key:'sticker_reply',name:'表情回复',detail:'按语境选用当前会话的收藏图片',group:true},
 {key:'memory',name:'长期记忆',detail:'自动提取与使用记忆；关闭后保留已有记录',group:true},
 {key:'persona_growth',name:'人设养成',detail:'学习并使用相处习惯；基础人设不变',group:false},
 {key:'proactive',name:'主动消息与延迟补充',detail:'按已配置的时段和频率主动聊天',group:true},
 {key:'bilibili',name:'B 站阅读',detail:'手动或定时读字幕，聊天时参考阅读心得',group:false},
 {key:'tools',name:'AI 工具操作',detail:'查询和编辑记录、联网搜索、下载、生成图片等',group:false},
]
const {data,loading,error,load}=useResource(()=>api<Permissions>('/permissions'),{enabled:true,features:{}})
const saving=ref(false), saveError=ref('')
const count=computed(()=>items.filter(i=>data.value.features[i.key]?.enabled).length)
async function change(key:string|null, field:keyof Grant, event:Event) {
 const input=event.target as HTMLInputElement,checked=input.checked
 input.checked=key?!!data.value.features[key]?.[field]:data.value.enabled
 if(saving.value)return
 const next=JSON.parse(JSON.stringify(data.value)) as Permissions
 if(key)next.features[key]![field]=checked;else next.enabled=checked
 saving.value=true;saveError.value=''
 try {data.value=await put<Permissions>('/permissions',next);notify('权限已生效')}
 catch(e){saveError.value=e instanceof Error?e.message:'保存失败，权限未改变'}
 finally{saving.value=false}
}
</script>
<template>
 <ResourceState :loading="loading" :error="error" @retry="load">
  <section class="permission-master"><span class="stat-icon lilac"><ShieldCheck :size="25" /></span><div class="grow"><h2>灵犀可以做什么，由你决定</h2><p>总开关控制 AI 功能；下面分别授权个人会话与 QQ 群聊。</p><small>已有历史、密钥和手动管理功能会保留。允许权限后，仍需配置相应连接和计划。</small></div><label class="control-switch"><input type="checkbox" role="switch" aria-label="AI 功能总开关" :checked="data.enabled" :disabled="saving" @change="change(null,'enabled',$event)" /><span aria-hidden="true" /></label></section>
  <div class="permission-summary"><span :class="['tag',data.enabled?'green':'neutral']">{{data.enabled?`已允许 ${count} 项功能`:'AI 功能已暂停'}}</span><span>切换后自动保存；拦截新的调用与尚未发送的消息。</span></div>
  <p v-if="saveError" role="alert" class="form-error">{{saveError}}</p>
  <div class="permission-table" role="table" aria-label="功能权限">
   <div class="permission-row permission-head" role="row"><span role="columnheader">功能</span><span role="columnheader">允许使用</span><span role="columnheader"><MessageCircle :size="16" />个人会话<small>网页 / QQ / 飞书 / AstrBot</small></span><span role="columnheader"><UsersRound :size="16" />QQ 群聊<small>仅已接入的群</small></span></div>
   <div v-for="item in items" :key="item.key" class="permission-row" role="row" :class="{'permission-muted':!data.enabled||!data.features[item.key]?.enabled}"><div role="cell"><strong>{{item.name}}</strong><p>{{item.detail}}</p></div><div v-for="field in (['enabled','private','group'] as const)" :key="field" role="cell"><label v-if="field!=='group'||item.group" class="control-switch"><input type="checkbox" role="switch" :aria-label="`${item.name} · ${field==='enabled'?'功能开关':field==='private'?'个人会话':'QQ 群聊'}`" :checked="data.features[item.key]?.[field]||false" :disabled="saving||!data.enabled||(field!=='enabled'&&!data.features[item.key]?.enabled)" @change="change(item.key,field,$event)" /><span aria-hidden="true" /></label><span v-else class="permission-unavailable">不开放</span></div></div>
  </div>
  <p class="muted small">个人会话使用主库，群聊使用各自上下文与独立人物库。群里不会开放私人工具或读取主人私聊。关闭识图后，主人仍可在允许收藏时明确命令保存图片。</p>
 </ResourceState>
</template>
