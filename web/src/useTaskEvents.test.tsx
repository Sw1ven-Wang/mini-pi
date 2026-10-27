import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useTaskEvents } from './useTaskEvents'
import type { TaskStatus } from './types'

class Source {
  static instances: Source[] = []
  onopen?: () => void
  onerror?: () => void
  addEventListener = vi.fn()
  removeEventListener = vi.fn()
  close = vi.fn()
  constructor() { Source.instances.push(this) }
}

afterEach(() => { vi.unstubAllGlobals(); Source.instances = [] })

it('keeps the event cursor across approval transitions and reconciles on reconnect', () => {
  vi.stubGlobal('EventSource', Source)
  const sync = vi.fn()
  const event = vi.fn()
  const { result, rerender } = renderHook(({ status }: { status: TaskStatus }) =>
    useTaskEvents('s', status, event, sync), { initialProps: { status: 'running' as TaskStatus } })
  const source = Source.instances[0]
  act(() => source.onopen?.())
  expect(sync).toHaveBeenCalledTimes(1)
  rerender({ status: 'awaiting_approval' })
  rerender({ status: 'running' })
  expect(Source.instances).toHaveLength(1)
  act(() => source.onerror?.())
  expect(result.current).toBe('reconnecting')
  expect(sync).toHaveBeenCalledTimes(2)
  act(() => source.onopen?.())
  expect(sync).toHaveBeenCalledTimes(3)
  rerender({ status: 'completed' })
  expect(source.close).toHaveBeenCalledTimes(1)
})

it('recreates a permanently closed stream after re-bootstrap', () => {
  vi.useFakeTimers()
  vi.stubGlobal('EventSource', Source)
  const sync = vi.fn()
  const { unmount } = renderHook(() => useTaskEvents('s', 'awaiting_approval', vi.fn(), sync))
  Object.defineProperty(Source.instances[0], 'readyState', { value: 2 })
  act(() => Source.instances[0].onerror?.())
  expect(sync).toHaveBeenCalledTimes(1)
  act(() => vi.advanceTimersByTime(1500))
  expect(Source.instances).toHaveLength(2)
  expect(Source.instances[0].close).toHaveBeenCalledTimes(1)
  unmount()
  vi.useRealTimers()
})
