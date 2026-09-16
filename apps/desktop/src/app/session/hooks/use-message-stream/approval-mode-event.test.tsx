import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $approvalModes, approvalModeForProfile } from '@/store/approval-mode'
import { $gateway } from '@/store/gateway'
import { respondToApprovalAction, setNativeNotifyEnabled, setNativeNotifyKind } from '@/store/native-notifications'
import { __resetNativeNotifyBaselineForTests } from '@/store/notify-baseline'
import { $activeGatewayProfile } from '@/store/profile'
import { clearAllPrompts } from '@/store/prompts'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const ACTIVE_SID = 'session-active'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(ACTIVE_SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

describe('live session.info approval mode reconciliation', () => {
  beforeEach(() => {
    handleEvent = null
    $approvalModes.set({})
    $activeGatewayProfile.set('work')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('reconciles an active-session event under its source gateway profile', async () => {
    await mountStream()

    act(() =>
      handleEvent!({
        payload: { approval_mode: 'off' },
        profile: 'work',
        session_id: ACTIVE_SID,
        type: 'session.info'
      })
    )

    expect(approvalModeForProfile('work')).toBe('off')
    expect(approvalModeForProfile('default')).toBe('smart')
  })

  it('ignores stale session.info from a non-active session on the active gateway', async () => {
    await mountStream()

    act(() =>
      handleEvent!({
        payload: { approval_mode: 'off' },
        profile: 'work',
        session_id: 'session-stale',
        type: 'session.info'
      })
    )

    expect(approvalModeForProfile('work')).toBe('smart')
  })

  it('does not cache an event under a different active profile when its source profile is absent', async () => {
    await mountStream()
    $activeGatewayProfile.set('personal')

    act(() => handleEvent!({ payload: { approval_mode: 'off' }, session_id: ACTIVE_SID, type: 'session.info' }))

    expect(approvalModeForProfile('personal')).toBe('smart')
    expect(approvalModeForProfile('work')).toBe('smart')
  })
})

describe('request-bound native approval notifications', () => {
  const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
  const originalDesktop = desktopWindow.hermesDesktop
  const notify = vi.fn().mockResolvedValue(true)
  const request = vi.fn().mockResolvedValue({ resolved: 1 })

  beforeEach(() => {
    handleEvent = null
    notify.mockClear()
    request.mockClear()
    $activeGatewayProfile.set('work')
    $gateway.set({ request } as unknown as ReturnType<typeof $gateway.get>)
    desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    setNativeNotifyEnabled(true)
    setNativeNotifyKind('approval', true)
    __resetNativeNotifyBaselineForTests()
  })

  afterEach(() => {
    cleanup()
    clearAllPrompts()
    $gateway.set(null)
    desktopWindow.hermesDesktop = originalDesktop
    vi.restoreAllMocks()
  })

  it('carries the event request ID through the OS action back to the gateway', async () => {
    await mountStream()
    act(() =>
      handleEvent!({
        payload: { command: 'fixture', description: 'approval', request_id: 'old-request' },
        profile: 'work',
        session_id: ACTIVE_SID,
        type: 'approval.request'
      })
    )
    expect(notify).toHaveBeenCalledTimes(1)
    const notification = notify.mock.calls[0][0]
    expect(notification.actions).toHaveLength(2)
    await respondToApprovalAction(notification.sessionId, notification.actions[0].id)
    expect(request).toHaveBeenCalledWith('approval.respond', {
      choice: 'once',
      request_id: 'old-request',
      session_id: ACTIVE_SID
    })
    await respondToApprovalAction(notification.sessionId, notification.actions[1].id)
    expect(request).toHaveBeenCalledWith('approval.respond', {
      choice: 'deny',
      request_id: 'old-request',
      session_id: ACTIVE_SID
    })
  })

  it('does not emit actionable buttons for a legacy event without an identity', async () => {
    await mountStream()
    act(() =>
      handleEvent!({
        payload: { command: 'fixture', description: 'approval' },
        profile: 'work',
        session_id: 'session-legacy-notification',
        type: 'approval.request'
      })
    )
    expect(notify).toHaveBeenCalledTimes(1)
    expect(notify.mock.calls[0][0].actions).toBeUndefined()
    expect(request).not.toHaveBeenCalledWith('approval.respond', expect.anything())
  })
})
