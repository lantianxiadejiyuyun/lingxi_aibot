<script setup lang="ts">
import type { Field } from '../lib/ui'
defineProps<{fields:Field[];busy:boolean}>()
const values=defineModel<Record<string,string>>({required:true})
</script>
<template><div class="form-fields"><div v-for="f in fields" :key="f.key" :class="['form-field',{wide:f.wide||f.type==='textarea'}]">
 <label><span>{{f.label}} <span v-if="f.required" class="required">*</span></span>
 <select v-if="f.type==='select'" v-model="values[f.key]" :required="f.required" :disabled="busy"><option v-for="[value,name] in f.options" :key="value" :value="value">{{name}}</option></select>
 <textarea v-else-if="f.type==='textarea'" v-model="values[f.key]" :required="f.required" :readonly="f.readonly" :disabled="busy" rows="5" />
 <input v-else v-model="values[f.key]" :type="f.type||'text'" :required="f.required" :min="f.min" :max="f.max" :step="f.step" :readonly="f.readonly" :disabled="busy" :autocomplete="f.type==='password'?'new-password':'off'" />
 <small v-if="f.hint">{{f.hint}}</small></label>
 <div v-if="f.presets" class="number-presets" :aria-label="f.label+'常用值'"><button v-for="[value,name] in f.presets" :key="value" type="button" :aria-label="f.label+' '+name" :aria-pressed="Number(values[f.key])===value" :disabled="busy||f.readonly" @click="values[f.key]=String(value)">{{name}}</button></div>
 </div></div></template>
<style scoped>.form-field{min-width:0}.number-presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.number-presets button{border:1px solid #ddd1e9;background:#f7f6f5;border-radius:7px;padding:5px 9px;font-size:11px;color:#65566d}.number-presets button:hover:not(:disabled),.number-presets button[aria-pressed="true"]{background:#f0dfea;border-color:#eabcdd;color:#57415e}</style>
