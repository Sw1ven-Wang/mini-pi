import { useEffect, useRef, useState } from 'react'
import type { AgentEvent, TaskStatus } from './types'

export type ConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed'

const terminal = new Set<TaskStatus>(['completed', 'failed', 'cancelled'])

export function useTaskEvents(
  sessionId: string | null,
  status: TaskStatus | null,
  onEvent: (event: AgentEvent) => void,
  onSync?: () => void,
): ConnectionState {
  const [connection, setConnection] = useState<ConnectionState>('idle')
  const [reconnectGeneration, setReconnectGeneration] = useState(0)
  const eventCallback = useRef(onEvent)
  const syncCallback = useRef(onSync)
  const active = Boolean(sessionId && status && !terminal.has(status))
  useEffect(() => {
    eventCallback.current = onEvent
    syncCallback.current = onSync
  }, [onEvent, onSync])

  useEffect(() => {
    if (!sessionId || !active) {
      setConnection(sessionId ? 'closed' : 'idle')
      return
    }
    setConnection('connecting')
    let retryTimer: number | undefined
    const source = new EventSource(`/api/tasks/${encodeURIComponent(sessionId)}/events`)
    source.onopen = () => {
      setConnection('live')
      // The replay window is bounded. Always reconcile with the authoritative
      // snapshot after connecting, including after an offline interval.
      syncCallback.current?.()
    }
    source.onerror = () => {
      setConnection('reconnecting')
      // A restarted server may reject the old cookie before onopen can fire.
      syncCallback.current?.()
      if (source.readyState === 2) {
        retryTimer = window.setTimeout(() => setReconnectGeneration((value) => value + 1), 1500)
      }
    }
    const listener = (message: MessageEvent<string>) => {
      try {
        eventCallback.current(JSON.parse(message.data) as AgentEvent)
      } catch {
        setConnection('reconnecting')
      }
    }
    source.addEventListener('agent', listener as EventListener)
    return () => {
      window.clearTimeout(retryTimer)
      source.removeEventListener('agent', listener as EventListener)
      source.close()
    }
  }, [sessionId, active, reconnectGeneration])

  return connection
}
