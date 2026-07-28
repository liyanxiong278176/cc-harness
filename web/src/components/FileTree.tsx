import { useEffect, useState } from 'react';
import { listFiles } from '../api/client';
import type { FileEntry } from '../api/types';

interface TreeNode extends FileEntry {
  children?: TreeNode[];
  expanded?: boolean;
}

export function FileTree({ sessionId, onSelect }: { sessionId: string; onSelect: (path: string) => void }) {
  const [tree, setTree] = useState<TreeNode[]>([]);

  useEffect(() => {
    listFiles(sessionId, '.').then(setTree).catch(console.error);
  }, [sessionId]);

  const toggle = async (idx: number) => {
    const node = tree[idx];
    if (node.type !== 'dir' || node.expanded) {
      const next = [...tree];
      next[idx] = { ...node, expanded: !node.expanded };
      setTree(next);
      return;
    }
    const children = await listFiles(sessionId, node.path);
    const next = [...tree];
    next[idx] = { ...node, expanded: true, children };
    setTree(next);
  };

  return (
    <div className="p-2 text-sm font-mono">
      <h3 className="text-xs font-semibold mb-2 text-gray-600">FILES</h3>
      <ul className="space-y-1">
        {tree.map((node, i) => (
          <li key={node.path}>
            {node.type === 'dir' ? (
              <button onClick={() => toggle(i)} className="text-left hover:bg-gray-100 w-full px-2 py-1 rounded">
                {node.expanded ? '▼' : '▶'} {node.name}/
              </button>
            ) : (
              <button
                onClick={() => onSelect(node.path)}
                className="text-left hover:bg-blue-100 w-full px-2 py-1 rounded"
              >
                📄 {node.name}
              </button>
            )}
            {node.expanded && node.children && (
              <ul className="ml-4 space-y-1">
                {node.children.map((c) => (
                  <li key={c.path}>
                    {c.type === 'dir' ? (
                      <span className="text-gray-500">📁 {c.name}/</span>
                    ) : (
                      <button
                        onClick={() => onSelect(c.path)}
                        className="text-left hover:bg-blue-100 w-full px-2 py-1 rounded"
                      >
                        📄 {c.name}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}