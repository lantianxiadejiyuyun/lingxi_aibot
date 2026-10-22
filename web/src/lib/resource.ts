import { onMounted, ref, shallowRef } from 'vue'
export function useResource<T>(loader: () => Promise<T>, initial: T) {
  const data = shallowRef(initial)
  const loading = ref(true)
  const error = ref('')
  let sequence = 0
  async function load() {
    const current = ++sequence
    loading.value = true
    error.value = ''
    try { const result = await loader(); if (current === sequence) data.value = result }
    catch (e) { if (current === sequence) error.value = e instanceof Error ? e.message : '加载失败' }
    finally { if (current === sequence) loading.value = false }
  }
  onMounted(load)
  return { data, loading, error, load }
}
