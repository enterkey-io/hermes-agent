import type { ApprovalRespondResponse } from '../gatewayTypes.js'

import type { GatewayRpc } from './interfaces.js'
import { getOverlayState, patchOverlayState } from './overlayStore.js'
import { patchTurnState } from './turnStore.js'
import { getUiState, patchUiState } from './uiStore.js'

export async function respondToApproval(
  rpc: GatewayRpc,
  requestId: string | undefined,
  sessionId: string | null,
  choice: string
): Promise<void> {
  if (!requestId) {return}

  const response = await rpc<ApprovalRespondResponse>('approval.respond', {
    choice,
    request_id: requestId,
    session_id: sessionId
  })

  if (!response || getUiState().sid !== sessionId || getOverlayState().approval?.requestId !== requestId) {return}

  patchOverlayState({ approval: null })
  patchTurnState({
    outcome: response.resolved ? (choice === 'deny' ? 'denied' : `approved (${choice})`) : 'approval expired'
  })
  patchUiState({ status: response.resolved ? 'running…' : 'approval expired' })
}
