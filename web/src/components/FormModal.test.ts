import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import FormModal from './FormModal.vue'
import { field, modal, openForm } from '../lib/ui'

Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { value() { this.setAttribute('open', '') }, configurable: true })
Object.defineProperty(HTMLDialogElement.prototype, 'close', { value() { this.removeAttribute('open') }, configurable: true })
afterEach(() => { modal.config = null })
it('keeps user input and server errors visible until save succeeds', async () => {
  const save = vi.fn().mockRejectedValueOnce(Error('版本冲突，请刷新后重试')).mockResolvedValueOnce(undefined)
  const wrapper = mount(FormModal)
  openForm({ title: '消费', fields: [{ ...field('amount','金额','','number',true), step: '0.01' }], save })
  await nextTick(); await nextTick()
  await wrapper.find('input').setValue('12.34')
  expect(wrapper.find('input').attributes('step')).toBe('0.01')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(wrapper.find('[role="alert"]').text()).toContain('版本冲突')
  expect((wrapper.find('input').element as HTMLInputElement).value).toBe('12.34')
  expect(wrapper.find('dialog').attributes()).toHaveProperty('open')
  await wrapper.find('form').trigger('submit'); await flushPromises()
  expect(wrapper.find('dialog').attributes()).not.toHaveProperty('open')
  expect(save.mock.calls[0][0].amount).toBe('12.34')
  wrapper.unmount()
})

it('applies numeric presets without submitting and preserves custom values', async () => {
 const save = vi.fn().mockResolvedValue(undefined), wrapper = mount(FormModal)
 openForm({title:'模型预算',fields:[{...field('context_window','上下文容量',200000,'number',true),presets:[[128000,'128K'],[1000000,'1M']]},{...field('max_output','最大输出',8192,'number',true),presets:[[4096,'4K'],[16384,'16K']]}],save})
 await nextTick(); await nextTick()
 expect((wrapper.findAll('input')[0]!.element as HTMLInputElement).value).toBe('200000')
 await wrapper.get('[aria-label="上下文容量 1M"]').trigger('click')
 await wrapper.get('[aria-label="最大输出 16K"]').trigger('click')
 expect(save).not.toHaveBeenCalled()
 expect(wrapper.get('[aria-label="最大输出 16K"]').attributes('aria-pressed')).toBe('true')
 await wrapper.findAll('input')[1]!.setValue('12000')
 expect(wrapper.get('[aria-label="最大输出 16K"]').attributes('aria-pressed')).toBe('false')
 await wrapper.get('form').trigger('submit'); await flushPromises()
 expect(save).toHaveBeenCalledWith({context_window:'1000000',max_output:'12000'})
 wrapper.unmount()
})
