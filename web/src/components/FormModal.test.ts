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
