import { memo, useMemo } from 'react'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({
  gfm: true,
  breaks: false,
})

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const html = useMemo(() => {
    try {
      const parsed = marked.parse(text, { async: false }) as string
      return DOMPurify.sanitize(parsed, {
        USE_PROFILES: { html: true },
        ADD_ATTR: ['target', 'rel'],
      })
    } catch {
      return ''
    }
  }, [text])
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
})
