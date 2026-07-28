import Editor from '@monaco-editor/react';
import { useEffect, useState } from 'react';
import { readFile } from '../api/client';

export function CodeViewer({ sessionId, path }: { sessionId: string; path: string }) {
  const [content, setContent] = useState('');
  const [language, setLanguage] = useState('plaintext');

  useEffect(() => {
    readFile(sessionId, path).then((r) => {
      setContent(r.content);
      setLanguage(r.language);
    }).catch(console.error);
  }, [sessionId, path]);

  return (
    <Editor
      key={`${sessionId}:${path}`}
      height="100%"
      language={language}
      value={content}
      options={{
        readOnly: true,
        minimap: { enabled: false },
        fontSize: 13,
      }}
    />
  );
}