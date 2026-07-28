import { create } from 'zustand';
import type { ServerEvent, SessionMeta } from '../api/types';

interface Message {
  type: 'thought' | 'action' | 'observation' | 'result' | 'l4_ask' | 'l2_refused' | 'error' | 'compaction';
  data: ServerEvent;
}

interface SessionStore {
  sessions: SessionMeta[];
  currentSessionId: string | null;
  messages: Record<string, Message[]>;
  pendingAsk: { ask_id: string; question: string } | null;

  setSessions: (s: SessionMeta[]) => void;
  setCurrent: (sid: string | null) => void;
  appendMessage: (sid: string, msg: Message) => void;
  clearMessages: (sid: string) => void;
  setPendingAsk: (a: { ask_id: string; question: string } | null) => void;
}

export const useSessionStore = create<SessionStore>((set) => ({
  sessions: [],
  currentSessionId: null,
  messages: {},
  pendingAsk: null,

  setSessions: (s) => set({ sessions: s }),
  setCurrent: (sid) => set({ currentSessionId: sid }),
  appendMessage: (sid, msg) =>
    set((state) => ({
      messages: {
        ...state.messages,
        [sid]: [...(state.messages[sid] ?? []), msg],
      },
    })),
  clearMessages: (sid) =>
    set((state) => ({ messages: { ...state.messages, [sid]: [] } })),
  setPendingAsk: (a) => set({ pendingAsk: a }),
}));
