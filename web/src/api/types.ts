export const PROTOCOL_VERSION = 1;

export interface EventBase { type: string; ts: number; }

export interface ThoughtEvent extends EventBase {
  type: 'thought';
  text: string;
  iteration: number;
}

export interface ActionEvent extends EventBase {
  type: 'action';
  name: string;
  args: Record<string, unknown>;
  iteration: number;
}

export interface ObservationEvent extends EventBase {
  type: 'observation';
  text: string;
  is_error: boolean;
  duration_ms: number;
  iteration: number;
}

export interface ResultEvent extends EventBase {
  type: 'result';
  text: string;
}

export interface DoneEvent extends EventBase {
  type: 'done';
  session_id: string;
  turn_idx: number;
  duration_ms: number;
}

export interface L4AskEvent extends EventBase {
  type: 'l4_ask';
  ask_id: string;
  question: string;
  tool_name: string;
  args: Record<string, unknown>;
}

export interface L2RefusedEvent extends EventBase {
  type: 'l2_refused';
  template: string;
}

export interface ModeEvent extends EventBase {
  type: 'mode';
  value: 'coding' | 'plan' | 'design' | 'chat';
}

export interface ErrorEvent extends EventBase {
  type: 'error';
  message: string;
  fatal: boolean;
}

export type ServerEvent = ThoughtEvent | ActionEvent | ObservationEvent | ResultEvent
  | DoneEvent | L4AskEvent | L2RefusedEvent | ModeEvent | ErrorEvent;

export interface UserInputEvent { type: 'user_input'; text: string; }
export interface SlashCommand { type: 'slash'; command: string; }
export interface L4ResponseEvent {
  type: 'l4_response';
  ask_id: string;
  decision: 'yes' | 'always' | 'no';
}
export interface InterruptEvent { type: 'interrupt'; }

export type ClientEvent = UserInputEvent | SlashCommand | L4ResponseEvent | InterruptEvent;

export interface SessionMeta {
  session_id: string;
  cwd: string;
  mode: 'coding' | 'plan' | 'design' | 'chat';
  created_at: number;
  last_active_at: number;
  status: 'active' | 'closed' | 'errored';
}

export interface FileEntry {
  name: string;
  path: string;
  type: 'file' | 'dir';
  size: number;
  mtime: number;
}
