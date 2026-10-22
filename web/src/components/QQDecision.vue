<script setup lang="ts">
import { ref } from 'vue'
import { Sparkles, Settings2, FlaskConical } from '@lucide/vue'
import { api, post, put } from '../lib/api'
import { field, openForm, notify, run } from '../lib/ui'
const emit=defineEmits<{changed:[]}>()
const props=defineProps<{settings?:{url?:string;model?:string;aliases?:string[];timeout_secs?:number;max_checks_per_hour?:number}}>()
const testing=ref(false), sample=ref('大肥鱼，你喜欢什么颜色呀？'), result=ref<{action:string;elapsed_ms:number;model:string}|null>(null), error=ref('')
const labels:Record<string,string>={reply:'适合接话',wait:'等下一条',ignore:'安静旁听'}
async function configure() {
  const current=await api<Record<string,any>>('/integrations'), c=current.qq?.decision||{}
  openForm({title:'本地接话判断',description:'使用独立的 Ollama 小模型，只读取当前群最近的公开消息。保存后，仍需在各群设置中选择观察或自动接话。',fields:[
    {...field('url','Ollama 地址',c.url||'http://ollama:11434','url',true),hint:'填写后端可以访问的局域网地址。Docker 内的 localhost 指向灵犀自身。'},
    field('model','本地模型',c.model||'qwen3:0.6b','text',true),
    {...field('aliases','机器人称呼',(c.aliases||['灵犀','大肥鱼','鱼鱼']).join('，'),'text',true),hint:'用逗号分隔，最多8个。'},
    {...field('timeout_secs','最长等待（秒）',c.timeout_secs??30,'number',true),min:3,max:60},
    {...field('max_checks_per_hour','每群每小时最多判断',c.max_checks_per_hour??60,'number',true),min:1,max:120,hint:'达到上限或服务繁忙时保持安静，被 @ 的回复仍正常处理。'},
  ],save:async v=>{
    const fresh=await api<Record<string,any>>('/integrations')
    await put('/integrations',{...fresh,qq:{...fresh.qq,decision:{url:v.url.trim(),model:v.model.trim(),aliases:v.aliases.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean),timeout_secs:Number(v.timeout_secs),max_checks_per_hour:Number(v.max_checks_per_hour)}}})
    result.value=null;error.value='';emit('changed');notify('本地判断配置已保存')
  }})
}
async function probe() {
  testing.value=true;error.value='';result.value=null
  try { result.value=await post('/qq/decision/probe',{text:sample.value}) }
  catch(e) { error.value=e instanceof Error?e.message:'测试失败' }
  finally { testing.value=false }
}
</script>
<template>
  <section class="decision-panel panel">
    <div class="section-heading"><div><span class="decision-eyebrow"><Sparkles :size="15" />先听一听，再开口</span><h2>让鱼鱼懂得什么时候接话</h2><p>明确叫名字时先按规则判断，其余语境交给本地小模型。每个群可以有自己的节奏。</p></div><button class="button secondary" @click="run(configure)"><Settings2 :size="15" />判断设置</button></div>
    <div class="decision-steps"><span><b>01</b> 听当前群的对话</span><span><b>02</b> 判断是否适合接话</span><span><b>03</b> 交给聊天模型回复</span></div>
    <details v-if="props.settings?.url" class="decision-test"><summary><FlaskConical :size="15" />测试一句话 · 不发送 QQ 消息 <small>{{props.settings.model}}</small></summary><form @submit.prevent="probe"><label for="decision-sample">测试消息</label><div class="decision-test-row"><input id="decision-sample" v-model="sample" maxlength="500" required /><button class="button" :disabled="testing">{{testing?'判断中…':'测试判断'}}</button></div></form><p v-if="error" role="alert">{{error}}</p><p v-if="result" role="status"><strong>{{labels[result.action]||result.action}}</strong> · {{(result.elapsed_ms/1000).toFixed(2)}} 秒 · {{result.model}}<br /><small>这里只测试单句。真实群聊还会参考上下文、冷却时间和频率限制。</small></p></details>
    <p v-else class="muted small">尚未连接本地模型。各群默认关闭自动接话，现有回复方式不变。</p>
  </section>
</template>
<style scoped>
.decision-panel{margin:22px 0 28px;background:linear-gradient(120deg,#f0dfea88,#f7f6f5 60%,#ddd1e955);border:1px solid #ddd1e9}
.decision-eyebrow{display:flex;align-items:center;gap:7px;font-size:12px;color:#736080;margin-bottom:12px;letter-spacing:.08em}
.decision-steps{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}
.decision-steps span{padding:12px 16px;border:1px solid #ddd1e9;border-radius:14px;background:#ffffff88;font-size:13px}
.decision-steps b{color:#687da5;margin-right:8px;font-size:11px}
.decision-test summary{cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px}
.decision-test summary small{margin-left:auto;color:#736080}.decision-test form{margin-top:18px}.decision-test label{font-size:12px;display:block;margin-bottom:8px}
.decision-test-row{display:flex;gap:10px}.decision-test-row input{flex:1;min-width:0}.decision-test [role=alert]{color:#9a3755}.decision-test [role=status]{line-height:1.8;font-size:13px}
@media(max-width:640px){.decision-panel>.section-heading{align-items:flex-start;flex-direction:column;gap:14px}.decision-steps{gap:8px}.decision-steps span{width:100%}.decision-test-row{flex-direction:column}}
</style>
