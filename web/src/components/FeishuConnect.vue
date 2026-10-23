<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { MessageCircle, RefreshCw, Link2 } from '@lucide/vue'
import { api, post, put, remove } from '../lib/api'
import { confirmAction, date, field, notify, openForm } from '../lib/ui'
import type { Profile } from '../types'
interface Status { managed?:boolean; enabled:boolean; configured:boolean; bound:boolean; state:string; sdk_version:string; heartbeat_at:number|null; spool_count:number; profile_id:string|null; conversation_id:string|null; events:{id:number;status:string;error:string;created_at:number}[] }
const data=ref<Status|null>(null), profiles=ref<Profile[]>([]), busy=ref(false), error=ref(''), command=ref(''), expires=ref(0), diagnosis=ref(''), dirty=ref(false)
const states:Record<string,string>={offline:'桥接服务未连接',idle:'等待连接',connecting:'正在连接飞书',connected:'飞书长连接正常',reconnecting:'正在重新连接',failed:'连接失败',disabled:'私聊已关闭',backend_unavailable:'后端暂时不可用'}
const eventNames:Record<string,string>={pending:'等待处理',queued:'已排队',processing:'正在回复',sent:'回复已送达',failed:'处理未完成',unknown:'结果待核实',cancelled:'已取消'}
async function load(){try{const next=await api<Status>('/feishu');if(dirty.value&&data.value){next.enabled=data.value.enabled;next.profile_id=data.value.profile_id}data.value=next}catch(e){error.value=e instanceof Error?e.message:'读取失败'}}
async function act(f:()=>Promise<void>){busy.value=true;error.value='';try{await f()}catch(e){error.value=e instanceof Error?e.message:'操作失败'}finally{busy.value=false}}
async function save(){if(!data.value)return;await put('/feishu',{enabled:data.value.enabled,profile_id:data.value.profile_id||null});command.value='';dirty.value=false;notify('飞书私聊设置已保存');await load()}
async function pair(){const r=await post<{command:string;expires_at:number}>('/feishu/pair');command.value=r.command;expires.value=r.expires_at}
async function diagnose(){const r=await post<{bot_enabled:boolean}>('/feishu/diagnose');diagnosis.value=r.bot_enabled?'应用认证通过，机器人已启用。接收与发送权限仍需通过实际私聊验证。':'应用认证通过，但机器人尚未启用，请检查发布状态。'}
function unbind(){confirmAction('解除飞书私聊绑定？','待处理消息将取消，会话历史保留。重新绑定后会创建新的会话。',()=>act(async()=>{await remove('/feishu/pair');command.value='';await load()}))}
function token(){confirmAction('生成新的桥接密钥？','旧桥接密钥会立即失效。生成后请更新 Docker 配置并重启飞书桥接服务。',()=>act(async()=>{const r=await post<{token:string}>('/feishu/bridge-token');openForm({title:'保存桥接密钥',description:'仅限飞书接入，不是账号 Token。填入 Docker 的 LINGXI_FEISHU_BRIDGE_TOKEN。只显示本次。',fields:[{...field('token','桥接密钥',r.token,'textarea'),readonly:true}],submit:'已保存',save:async()=>{}})}))}
let timer:ReturnType<typeof setInterval>|undefined
onMounted(async()=>{await load();try{profiles.value=await api<Profile[]>('/profiles')}catch{}timer=setInterval(()=>{if(!busy.value)void load()},15000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
 <section class="panel spaced feishu-connect">
  <div class="section-heading"><div><span class="tag neutral"><MessageCircle :size="13"/>飞书私聊</span><h2>换个地方，也能接着聊</h2><p>绑定你的飞书身份，文字对话、工具和记忆都留在主库。</p></div><button class="icon-button" aria-label="刷新飞书状态" @click="load"><RefreshCw :size="17"/></button></div>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <template v-if="data">
   <div class="feishu-status"><span v-if="data.managed" class="tag lilac">已内置飞书 SDK</span><span :class="['tag',data.state==='connected'?'green':'neutral']">{{ data.managed&&data.state==='offline'?'内置飞书服务等待连接':states[data.state]||data.state }}</span><span :class="['tag',data.bound?'green':'neutral']">{{ data.bound?'主人已绑定':'等待主人绑定' }}</span></div>
   <p v-if="data.managed" class="muted small">填写应用凭据并开启私聊后，飞书服务会自动连接。无需另行部署或复制桥接密钥。</p>
   <p v-if="!data.configured" class="muted">先在<RouterLink to="/settings?tab=channels">通知设置</RouterLink>填写飞书应用的 App ID 和 App Secret，再开启私聊。</p>
   <form class="feishu-options" @change="dirty=true" @submit.prevent="act(save)"><label><input v-model="data.enabled" type="checkbox" :disabled="!data.configured||busy"/>允许在飞书与我聊天</label><label>对话模型<select v-model="data.profile_id" :disabled="busy"><option :value="null">沿用默认模型</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{p.name}}</option></select></label><button class="button secondary" :disabled="busy||!data.configured">保存</button></form>
   <div class="feishu-actions"><button class="button secondary" :disabled="busy||!data.configured" @click="act(diagnose)">检查应用连接</button><button class="button" :disabled="busy||!data.enabled||!data.configured" @click="act(pair)"><Link2 :size="15"/>{{data.bound?'重新绑定':'生成绑定码'}}</button><button v-if="data.bound" class="text-link danger" :disabled="busy" @click="unbind">解除绑定</button></div>
   <p v-if="diagnosis" role="status" class="muted">{{diagnosis}}</p>
   <div v-if="command" class="feishu-pair"><strong>复制下面这句，私聊发给飞书机器人</strong><input :value="command" readonly aria-label="飞书绑定命令" @focus="($event.target as HTMLInputElement).select()"/><small>有效至 {{date(expires)}}，只使用一次。绑定后直接发文字即可。</small></div>
   <p class="muted small">{{data.sdk_version?'官方 SDK '+data.sdk_version+' · ':''}}{{data.heartbeat_at?'最近心跳 '+date(data.heartbeat_at):'等待桥接服务心跳'}}<span v-if="data.spool_count"> · {{data.spool_count}} 条消息等待转交</span></p>
   <details><summary>{{data.managed?'飞书应用与绑定说明':'接入说明与桥接配置'}}</summary><ol><li>在飞书开发者后台启用应用机器人并发布版本。</li><li>事件与回调选择“长连接”，订阅 <code>im.message.receive_v1</code>。申请接收单聊消息和以应用身份发送消息的权限。</li><li v-if="!data.managed">部署项目的飞书桥接 Docker 服务，填写专用桥接密钥。</li><li v-else>在通知设置中填写飞书应用凭据，保存并开启私聊，等待连接状态更新。</li><li>在这里生成绑定码，私聊发给机器人完成绑定。</li></ol><p class="muted small">只接收绑定主人的私聊文字；群聊不接入个人记忆。图片、语音和文件暂不解析。每个飞书应用只运行一个桥接实例。</p><button v-if="!data.managed" class="text-link" :disabled="busy" @click="token">生成或轮换桥接密钥</button></details>
   <div v-if="data.events.length" class="feishu-events"><h3>最近的接收记录</h3><article v-for="e in data.events" :key="e.id"><span>{{eventNames[e.status]||e.status}}</span><time>{{date(e.created_at)}}</time><small v-if="e.error">{{e.error}}</small></article></div>
  </template>
 </section>
</template>
<style scoped>
.feishu-connect{background:linear-gradient(135deg,#f7f6f5,#f0dfea55)}.feishu-status,.feishu-actions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.feishu-options{display:flex;align-items:end;gap:18px;flex-wrap:wrap;margin:20px 0}.feishu-options label{display:flex;gap:10px;align-items:center}.feishu-options select{min-width:150px;max-width:100%}.feishu-options input[type=checkbox]{width:auto}.feishu-pair{padding:18px;background:#ddd1e950;border:1px solid #ddd1e9;border-radius:14px;display:grid;gap:10px;margin:16px 0}.feishu-pair input{font-family:monospace;min-width:0;width:100%;box-sizing:border-box}.feishu-connect details{margin-top:18px}.feishu-connect details li{line-height:1.8;margin:6px 0}.feishu-connect summary{cursor:pointer}.feishu-events{border-top:1px solid #ddd1e9;margin-top:20px;padding-top:16px}.feishu-events article{display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;padding:10px 0}.feishu-events small{flex-basis:100%}.feishu-events time{color:var(--muted);font-size:12px}@media(max-width:600px){.feishu-options{align-items:stretch;flex-direction:column}.feishu-options label{justify-content:space-between}}
</style>
