import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApprovalDialog, DiffViewer, PhaseRail } from './components'

describe('workbench components', () => {
  it('renders the active agent phase accessibly', () => {
    render(<PhaseRail phase="edit" status="running" />)
    expect(screen.getByText('Edit').closest('li')).toHaveAttribute('aria-current', 'step')
    expect(screen.getByText('Plan').closest('li')).toHaveClass('done')
  })

  it('switches between bounded mutation previews', async () => {
    const user = userEvent.setup()
    render(<DiffViewer preview={{
      preview_id: 'p1',
      truncated: false,
      operations: [
        { kind: 'edit_file', path: 'src/a.py', source: null, destination: null, diff: '-old\n+new', truncated: false },
        { kind: 'move_file', path: null, source: 'src/b.py', destination: 'src/c.py', diff: '', truncated: false },
      ],
    }} />)
    expect(screen.getByText('-old')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /src\/b.py/ }))
    expect(screen.getAllByText('src/c.py').length).toBeGreaterThan(0)
  })

  it('returns the exact approval decision selected by the user', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<ApprovalDialog
      approval={{
        approval_id: 'a1', tool_name: 'workspace_mutation', reason: 'write',
        rule_id: 'default_mutation_ask', matched_rule_ids: [], risk: 'write',
        normalized_targets: ['src/app.py'], summary: 'Edit src/app.py', preview_id: null,
      }}
      preview={null}
      busy={false}
      onDecision={onDecision}
    />)
    await user.click(screen.getByRole('button', { name: '允许一次' }))
    expect(onDecision).toHaveBeenCalledWith('approve_once')
  })
})
