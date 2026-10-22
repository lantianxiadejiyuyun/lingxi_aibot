import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import StickerImage from './StickerImage.vue'
import { setSession } from '../lib/api'

afterEach(() => { setSession('', null); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('authenticates image requests and ignores a stale response after changing the preview', async () => {
  setSession('test-private-image-token', null)
  let finishOld!: (value: Response) => void
  const request = vi.fn()
    .mockImplementationOnce(() => new Promise<Response>(resolve => { finishOld = resolve }))
    .mockResolvedValueOnce(new Response(new Blob(['original'])))
  const create = vi.fn().mockReturnValue('blob:private-original'), revoke = vi.fn()
  vi.stubGlobal('fetch', request)
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
  const wrapper = mount(StickerImage, { props: { id: 'image-1', title: '收藏', original: false } })
  expect(request.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer test-private-image-token' })
  await wrapper.setProps({ original: true }); await flushPromises()
  expect(request.mock.calls[0][1].signal.aborted).toBe(true)
  expect(wrapper.find('img').attributes('src')).toBe('blob:private-original')
  finishOld(new Response(new Blob(['stale thumbnail']))); await flushPromises()
  expect(create).toHaveBeenCalledTimes(1)
  wrapper.unmount()
  expect(revoke).toHaveBeenCalledWith('blob:private-original')
  expect(request.mock.calls[1][1].signal.aborted).toBe(true)
})

it('shows an unavailable state for inaccessible images instead of creating a public image URL', async () => {
  const create = vi.fn()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })))
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: vi.fn() })
  const wrapper = mount(StickerImage, { props: { id: 'missing', title: '图片' } })
  await flushPromises()
  expect(wrapper.find('[role="status"]').text()).toBe('图片暂不可用')
  expect(wrapper.find('img').exists()).toBe(false)
  expect(create).not.toHaveBeenCalled()
  wrapper.unmount()
})
