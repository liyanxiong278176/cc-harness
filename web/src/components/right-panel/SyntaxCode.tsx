import type { ReactNode } from 'react'

const TOKEN_PATTERN = /(\/\*.*?\*\/|\/\/[^\r\n]*|#[^\r\n]*|<!--.*?-->|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b\d+(?:\.\d+)?\b|\b(?:async|await|break|case|catch|class|const|continue|def|else|export|extends|false|finally|for|from|function|if|import|in|interface|let|new|None|null|of|pass|private|public|return|self|static|super|this|throw|true|try|type|undefined|var|while|yield)\b)/g

function tokenClass(token: string) {
  if (/^(?:\/\/|#|\/\*|<!--)/.test(token)) return 'syntax-comment'
  if (/^["'`]/.test(token)) return 'syntax-string'
  if (/^\d/.test(token)) return 'syntax-number'
  return 'syntax-keyword'
}

function renderLine(line: string): ReactNode[] {
  const output: ReactNode[] = []
  line.split(TOKEN_PATTERN).forEach((part, index) => {
    if (!part) return
    const highlighted = TOKEN_PATTERN.test(part)
    TOKEN_PATTERN.lastIndex = 0
    output.push(highlighted ? <span className={tokenClass(part)} key={index}>{part}</span> : part)
  })
  return output
}

export function SyntaxCode({ content }: { content: string }) {
  return <pre className="artifact-syntax-code"><code>{content.split('\n').map((line, index) => <span className="syntax-line" key={index}><span className="syntax-line-number" aria-hidden="true">{index + 1}</span><span className="syntax-line-content">{renderLine(line)}</span></span>)}</code></pre>
}
