import { beforeEach, describe, expect, it, vi } from 'vitest'

import { respondToApproval } from '../app/approvalResponse.js'
import type { GatewayRpc } from '../app/interfaces.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

beforeEach(() => {
  resetOverlayState()
  resetTurnState()
  resetUiState()
  patchUiState({ sid: 'session-1', status: 'waiting' })
  patchOverlayState({ approval: { command: 'fixture', description: 'approval', requestId: 'request-1' } })
})

describe('request-bound approval responses', () => {
  it.each(['once', 'deny'])('sends %s for the displayed request and reports success', async choice => {
    const rpc = vi.fn().mockResolvedValue({ resolved: 1 })
    await respondToApproval(rpc as GatewayRpc, 'request-1', 'session-1', choice)
    expect(rpc).toHaveBeenCalledWith('approval.respond', {
      choice,
      request_id: 'request-1',
      session_id: 'session-1'
    })
    expect(getOverlayState().approval).toBeNull()
    expect(getTurnState().outcome).toBe(choice === 'deny' ? 'denied' : 'approved (once)')
  })

  it.each([undefined, ''])('never submits an unbound legacy prompt (%s)', async requestId => {
    const rpc = vi.fn()
    await respondToApproval(rpc as GatewayRpc, requestId, 'session-1', 'once')
    expect(rpc).not.toHaveBeenCalled()
    expect(getOverlayState().approval?.requestId).toBe('request-1')
  })

  it('reports expiry rather than approval when no request resolved', async () => {
    const rpc = vi.fn().mockResolvedValue({ resolved: 0 })
    await respondToApproval(rpc as GatewayRpc, 'request-1', 'session-1', 'once')
    expect(getOverlayState().approval).toBeNull()
    expect(getTurnState().outcome).toBe('approval expired')
    expect(getUiState().status).toBe('approval expired')
  })

  it.each(['new-request', 'new-session'])('preserves %s during an in-flight response', async replacement => {
    let finish!: (result: { resolved: number }) => void

    const rpc = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )

    const pending = respondToApproval(rpc as GatewayRpc, 'request-1', 'session-1', 'deny')
    const nextId = replacement === 'new-request' ? 'request-2' : 'request-1'
    patchOverlayState({ approval: { command: 'new fixture', description: 'new', requestId: nextId } })
    patchUiState({ sid: replacement === 'new-session' ? 'session-2' : 'session-1', status: 'new pending' })
    finish({ resolved: 1 })
    await pending
    expect(getOverlayState().approval?.command).toBe('new fixture')
    expect(getUiState().status).toBe('new pending')
    expect(getTurnState().outcome).not.toBe('denied')
  })

  it('keeps the prompt on a failed transport response', async () => {
    await respondToApproval(vi.fn().mockResolvedValue(null) as GatewayRpc, 'request-1', 'session-1', 'once')
    expect(getOverlayState().approval?.requestId).toBe('request-1')
    expect(getUiState().status).toBe('waiting')
  })
})
