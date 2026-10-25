<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { X, LoaderCircle } from '@lucide/vue'
import { modal } from '../lib/ui'
import FormFields from './FormFields.vue'
const dialog = ref<HTMLDialogElement>()
const values = ref<Record<string, string>>({})
const busy = ref(false)
const error = ref('')
const advanced=ref(false)
const basicFields=computed(()=>modal.config?.fields?.filter(f=>!f.advanced)||[])
const advancedFields=computed(()=>modal.config?.fields?.filter(f=>f.advanced)||[])
watch(() => modal.version, async () => {
  values.value = Object.fromEntries((modal.config?.fields || []).map(f => [f.key, String(f.value ?? '')]))
  error.value = ''
  advanced.value = false
  await nextTick()
  dialog.value?.showModal()
})
function close() { if (!busy.value) { dialog.value?.close(); modal.config = null } }
async function revealInvalid(e:Event){const target=e.target as HTMLInputElement;if(target.closest('.form-advanced')){advanced.value=true;await nextTick();target.focus()}}
async function submit() {
  if (!modal.config || busy.value) return
  const version = modal.version
  busy.value = true
  error.value = ''
  try {
    await modal.config.save(Object.fromEntries(Object.entries(values.value).map(([key, value]) => [key, String(value)])))
    if (version === modal.version) { dialog.value?.close(); modal.config = null }
  } catch (e) { error.value = e instanceof Error ? e.message : '保存失败' }
  finally { busy.value = false }
}
</script>
<template>
  <dialog ref="dialog" class="form-dialog" aria-labelledby="dialog-title" @cancel.prevent="close" @click="($event.target === dialog) && close()">
    <form v-if="modal.config" @submit.prevent="submit" @invalid.capture="revealInvalid">
      <div class="modal-heading"><div><span class="eyebrow">YOUR SPACE, YOUR WAY</span><h2 id="dialog-title">{{ modal.config.title }}</h2></div><button type="button" class="icon-button" aria-label="关闭弹窗" :disabled="busy" @click="close"><X /></button></div>
      <p v-if="modal.config.description" class="modal-description">{{ modal.config.description }}</p>
      <FormFields v-model="values" :fields="basicFields" :busy="busy" />
      <details v-if="advancedFields.length" class="form-advanced" :open="advanced" @toggle="advanced=($event.target as HTMLDetailsElement).open"><summary>高级设置 · {{advancedFields.length}} 项<span>按需调整，折叠时保留已有值</span></summary><FormFields v-model="values" :fields="advancedFields" :busy="busy" /></details>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <div class="modal-footer"><button class="button secondary" type="button" :disabled="busy" @click="close">取消</button><button class="button" :class="{ destructive: modal.config.danger }" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ busy ? '正在处理…' : modal.config.submit || '保存更改' }}</button></div>
    </form>
  </dialog>
</template>
<style scoped>
.form-advanced{margin-top:22px;border-top:1px solid #ddd1e9;padding-top:18px}.form-advanced summary{cursor:pointer;font-size:13px;color:#65566d}.form-advanced summary span{display:block;margin:7px 0 0 16px;font-size:11px;color:#8b7f91}.form-advanced[open]>.form-fields{margin-top:20px}
</style>
