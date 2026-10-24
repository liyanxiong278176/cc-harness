import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import type { Approval } from './types'

export function ApprovalCard({
  approvals, toolLabel, onApprove, onReject,
}: {
  approvals: Approval[]
  toolLabel: (name?: string) => string
  onApprove: (approval: Approval) => Promise<void>
  onReject: (approval: Approval) => Promise<void>
}) {
  const [busy, setBusy] = useState<string | null>(null)
  if (approvals.length === 0) return null

  async function act(approval: Approval, action: 'approve' | 'reject') {
    setBusy(`${approval.approval_id}:${action}`)
    try {
      if (action === 'approve') await onApprove(approval)
      else await onReject(approval)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="approval-stack">
      {approvals.map(approval => (
        <section className="approval-card" key={approval.approval_id}>
          <div className="approval-copy">
            <span className="approval-icon"><ShieldAlert size={17} /></span>
            <div>
              <div className="approval-title">需要你的批准</div>
              <div className="approval-detail">
                {toolLabel(approval.tool_name || approval.scope?.[0])}
                {approval.scope && approval.scope.filter(value => ![
                  'run_command', 'Read', 'Write', 'Edit', 'Grep', 'Glob',
                ].includes(value)).length > 0
                  ? ` · ${approval.scope.filter(value => ![
                    'run_command', 'Read', 'Write', 'Edit', 'Grep', 'Glob',
                  ].includes(value)).slice(0, 3).join(', ')}`
                  : ''}
              </div>
            </div>
          </div>
          <div className="approval-actions">
            <button
              type="button"
              className="button ghost"
              disabled={busy !== null}
              onClick={() => void act(approval, 'reject')}
            >
              拒绝
            </button>
            <button
              type="button"
              className="button primary"
              disabled={busy !== null}
              onClick={() => void act(approval, 'approve')}
            >
              {busy === `${approval.approval_id}:approve` ? '批准中…' : '允许'}
            </button>
          </div>
        </section>
      ))}
    </div>
  )
}
