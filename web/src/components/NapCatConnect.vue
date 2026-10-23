<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowUpRight, Copy, MessageCircle, RefreshCw } from '@lucide/vue'
import { post, session } from '../lib/api'
import { deploymentUrl, getDeployment, type DeploymentInfo } from '../lib/deployment'
import { notify } from '../lib/ui'
import { handoffNapcat } from '../lib/napcat'

defineProps<{ compact?: boolean }>()
const emit = defineEmits<{ configured: [] }>()
const data = ref<DeploymentInfo | null>(null), loading = ref(false), configuring = ref(false), opening = ref(false), error = ref('')
const webui = computed(() => deploymentUrl(data.value?.napcat?.webui_url))
const onebot = computed(() => deploymentUrl(data.value?.napcat?.http_url))
const callback = computed(() => deploymentUrl(data.value?.napcat?.callback_url || `/api/v2/qq/inbound/${encodeURIComponent(session.user?.id || '')}`))
const internalCallback = computed(() => deploymentUrl(data.value?.napcat?.internal_callback_url))
let cancelLogin: (() => void) | null = null
async function load() { loading.value = true; data.value = await getDeployment(); loading.value = false }
async function login() {
  if (!session.user?.is_admin || !data.value?.napcat?.login_available || !webui.value || opening.value) return
  opening.value = true; error.value = ''
  let popup: Window | null = null
  const controller = new AbortController()
  cancelLogin = () => { controller.abort(); popup?.close() }
  try {
    popup = window.open('about:blank', '_blank')
    if (!popup) throw new Error('浏览器阻止了新窗口，请允许本站打开弹窗后重试。')
    const result = await post<{ url: string; credential: string }>('/deployment/napcat/login')
    if (controller.signal.aborted) return
    const url = deploymentUrl(result.url)
    if (!url || new URL(url).origin !== new URL(webui.value).origin || new URL(url).search || new URL(url).hash || typeof result.credential !== 'string' || !result.credential || result.credential.length > 8192) throw new Error('NapCat 登录地址无效，请刷新后重试。')
    await handoffNapcat(popup, new URL(url), result.credential, controller.signal)
  } catch (e) {
    popup?.close()
    if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '暂时无法进入 NapCat，请稍后重试。'
  } finally { cancelLogin = null; opening.value = false }
}
async function configure() {
  configuring.value = true; error.value = ''
  try {
    await post('/deployment/napcat/configure')
    notify('内置 NapCat 连接已保存，请继续填写 QQ 身份并开启接入')
    emit('configured')
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '连接配置未保存，请重试' }
  finally { configuring.value = false }
}
async function copy(value: string, input: HTMLInputElement | null) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value)
    else {
      input?.focus(); input?.select()
      if (!input || !document.execCommand('copy')) throw new Error()
    }
    notify('地址已复制')
  } catch {
    input?.focus(); input?.select()
    notify('请复制已选中的地址')
  }
}
onMounted(load)
onBeforeUnmount(() => cancelLogin?.())
</script>
<template>
  <section v-if="session.user?.is_admin" :class="['napcat-connect', { panel: !compact }]">
    <div class="section-heading">
      <div><span class="tag neutral"><MessageCircle :size="13" />NapCat</span><h3>QQ 连接入口</h3></div>
      <button class="icon-button" :disabled="loading" aria-label="刷新 NapCat 地址" @click="load"><RefreshCw :size="16" /></button>
    </div>
    <p class="muted small">{{ data?.napcat?.login_available ? '直接进入 NapCat，不用查日志或手动输入管理密钥。首次连接 QQ 仍需本人扫码登录。' : data?.napcat?.managed ? '随灵犀一起部署。打开管理页面完成 QQ 登录，再配置私聊或群接入。' : '打开 NapCat 完成 QQ 登录，使用下方地址连接灵犀。' }}</p>
    <div class="napcat-actions"><button v-if="webui && data?.napcat?.login_available" class="button secondary" :disabled="opening" @click="login">{{ opening ? '正在进入…' : '一键进入 NapCat' }}<ArrowUpRight :size="15" /></button><a v-else-if="webui" :href="webui" target="_blank" rel="noopener noreferrer" class="button secondary">打开 NapCat 管理<ArrowUpRight :size="15" /></a><button v-if="data?.napcat?.managed && data.napcat.configure_available" class="button secondary" :disabled="configuring" @click="configure">{{ configuring ? '正在配置…' : '使用内置 NapCat' }}</button></div>
    <p v-if="!webui" class="muted small">{{ loading ? '正在读取部署地址…' : '尚未设置 NapCat 管理地址，请在部署配置中填写公开管理地址。' }}</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <a v-if="error && webui && data?.napcat?.login_available" :href="webui" target="_blank" rel="noopener noreferrer" class="text-link">手动打开 NapCat 管理<ArrowUpRight :size="15" /></a>
    <details class="napcat-addresses">
      <summary>查看与复制连接地址</summary>
      <div v-for="item in [
        { label: 'NapCat 管理地址', value: webui },
        { label: 'OneBot HTTP 地址（灵犀中填写）', value: onebot },
        { label: 'HTTP 客户端回调（外部 NapCat）', value: callback },
        { label: 'HTTP 客户端回调（同一部署内）', value: internalCallback !== callback ? internalCallback : '' },
      ].filter(item => item.value)" :key="item.label" class="napcat-address">
        <label>{{ item.label }}<span><input :value="item.value" readonly :aria-label="item.label" @focus="($event.target as HTMLInputElement).select()" /><button class="icon-button" :aria-label="'复制' + item.label" @click="copy(item.value, ($event.currentTarget as HTMLElement).parentElement?.querySelector('input') || null)"><Copy :size="15" /></button></span></label>
      </div>
      <p class="muted small">NapCat 的 HTTP 客户端使用对应回调地址，Token 填事件回调密钥，消息格式选 array。HTTP 服务端的 Token 填入灵犀的 OneBot 访问 Token。</p>
    </details>
  </section>
</template>
<style scoped>
.napcat-actions{display:flex;gap:10px;flex-wrap:wrap}
.napcat-connect:not(.panel){margin-top:22px;border-top:1px solid #ddd1e9;padding-top:18px;background:none}
.napcat-connect{min-width:0;background:linear-gradient(135deg,#f7f6f5,#f0dfea45)}.napcat-connect h3{margin:10px 0 0}.napcat-connect>.muted{line-height:1.7}.napcat-addresses{margin-top:18px}.napcat-addresses summary{cursor:pointer;font-size:13px;line-height:1.7}.napcat-address{margin-top:16px}.napcat-address label{display:grid;gap:7px;font-size:12px;color:var(--muted)}.napcat-address span{display:flex;gap:7px;align-items:center;min-width:0}.napcat-address input{width:100%;min-width:0;font-family:monospace;font-size:12px}.napcat-address .icon-button{flex-shrink:0}.napcat-addresses>.muted{margin-top:16px;line-height:1.7}
</style>
