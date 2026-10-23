<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { X, LoaderCircle } from '@lucide/vue'
import { modal } from '../lib/ui'
const dialog = ref<HTMLDialogElement>()
const values = ref<Record<string, string>>({})
const busy = ref(false)
const error = ref('')
watch(() => modal.version, async () => {
  values.value = Object.fromEntries((modal.config?.fields || []).map(f => [f.key, String(f.value ?? '')]))
  error.value = ''
  await nextTick()
  dialog.value?.showModal()
})
function close() { if (!busy.value) { dialog.value?.close(); modal.config = null } }
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
    <form v-if="modal.config" @submit.prevent="submit">
      <div class="modal-heading"><div><span class="eyebrow">YOUR SPACE, YOUR WAY</span><h2 id="dialog-title">{{ modal.config.title }}</h2></div><button type="button" class="icon-button" aria-label="关闭弹窗" :disabled="busy" @click="close"><X /></button></div>
      <p v-if="modal.config.description" class="modal-description">{{ modal.config.description }}</p>
      <div class="form-fields">
        <div v-for="f in modal.config.fields" :key="f.key" :class="['form-field', { wide: f.wide || f.type === 'textarea' }]">
         <label>
          <span>{{ f.label }} <span v-if="f.required" class="required">*</span></span>
          <select v-if="f.type === 'select'" v-model="values[f.key]" :required="f.required" :disabled="busy"><option v-for="[value, name] in f.options" :key="value" :value="value">{{ name }}</option></select>
          <textarea v-else-if="f.type === 'textarea'" v-model="values[f.key]" :required="f.required" :readonly="f.readonly" :disabled="busy" rows="5" />
          <input v-else v-model="values[f.key]" :type="f.type || 'text'" :required="f.required" :min="f.min" :max="f.max" :step="f.step" :readonly="f.readonly" :disabled="busy" :autocomplete="f.type === 'password' ? 'new-password' : 'off'" />
          <small v-if="f.hint">{{ f.hint }}</small>
         </label>
         <div v-if="f.presets" class="number-presets" :aria-label="f.label + '常用值'">
          <button v-for="[value, name] in f.presets" :key="value" type="button" :aria-label="f.label + ' ' + name" :aria-pressed="Number(values[f.key]) === value" :disabled="busy || f.readonly" @click="values[f.key] = String(value)">{{ name }}</button>
         </div>
        </div>
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <div class="modal-footer"><button class="button secondary" type="button" :disabled="busy" @click="close">取消</button><button class="button" :class="{ destructive: modal.config.danger }" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ busy ? '正在处理…' : modal.config.submit || '保存更改' }}</button></div>
    </form>
  </dialog>
</template>
<style scoped>
.form-field{min-width:0}.number-presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.number-presets button{border:1px solid #ddd1e9;background:#f7f6f5;border-radius:7px;padding:5px 9px;font-size:11px;color:#65566d}.number-presets button:hover:not(:disabled),.number-presets button[aria-pressed="true"]{background:#f0dfea;border-color:#eabcdd;color:#57415e}
</style>
