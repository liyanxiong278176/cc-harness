import { memo, useEffect, useMemo, useState } from 'react'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { ChevronRight, LoaderCircle } from 'lucide-react'
import type { StreamingState } from '../../state/streaming'
import './StreamingMessage.css'

function safeMarkdown(source: string) {
  return DOMPurify.sanitize(marked.parse(source, { async: false }) as string)
}

export const StreamingMessage = memo(function StreamingMessage({
  state,
  text,
}: {
  state: StreamingState
  text: string
}) {
  const [thinkingOpen, setThinkingOpen] = useState(true)
  const html = useMemo(() => safeMarkdown(text), [text])
  const reasoningActive = state.phase === 'reasoning' && !state.frozen
  const canCollapse = state.reasoning.length > 0

  useEffect(() => {
    if (!canCollapse) return
    if (reasoningActive) setThinkingOpen(true)
    else setThinkingOpen(false)
  }, [canCollapse, reasoningActive, state.phase])

  const thinkingLabel = reasoningActive
    ? '思考中…'
    : state.reasoning_seconds === undefined
      ? '已思考'
      : `已思考 ${state.reasoning_seconds} 秒`
  const showCursor = !state.frozen && !['done', 'stopped', 'failed'].includes(state.phase)

  return (
    <>
      {canCollapse && (
        <section className={'stream-thinking ' + (reasoningActive ? 'active' : '')}>
          <button
            type="button"
            className="stream-thinking-toggle"
            aria-expanded={thinkingOpen}
            onClick={() => setThinkingOpen((open) => !open)}
          >
            <ChevronRight size={14} className={thinkingOpen ? 'expanded' : ''} />
            <span>{thinkingLabel}</span>
            {reasoningActive && <span className="stream-thinking-glow" aria-hidden="true" />}
          </button>
          {thinkingOpen && <div className="stream-thinking-content">{state.reasoning}</div>}
        </section>
      )}
      {state.phase === 'tool' && (
        <div className="streaming-tool-card" role="status">
          <LoaderCircle size={14} className="spin" />
          <span>正在调用 {state.tool_name || '工具'}…</span>
        </div>
      )}
      {text && <div className="markdown message-content" dangerouslySetInnerHTML={{ __html: html }} />}
      {showCursor && <span className="streaming-cursor" aria-label="正在生成" />}
    </>
  )
})
