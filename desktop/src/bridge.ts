import { Command, Child } from "@tauri-apps/plugin-shell";

export type BridgeMessage = {
  protocol_version: number;
  message_type: "response" | "event";
  request_id?: string | null;
  ok?: boolean;
  data?: Record<string, unknown>;
  error?: { code: string; message: string; error_type?: string };
  watch_id?: string | null;
  run_id?: string;
  sequence?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
};

type Pending = {
  resolve: (message: BridgeMessage) => void;
  reject: (error: Error) => void;
};

export class DesktopBridge {
  private child: Child | null = null;
  private readonly pending = new Map<string, Pending>();
  private readonly listeners = new Set<(message: BridgeMessage) => void>();
  private nextId = 1;

  async start(cwd: string): Promise<BridgeMessage> {
    if (this.child) return this.request("hello");
    const command = Command.sidecar("binaries/cc-harness-desktop-bridge", ["--cwd", cwd]);
    command.stdout.on("data", (line) => this.receive(String(line)));
    command.stderr.on("data", (line) => console.warn("cc-harness bridge:", line));
    command.on("close", (event) => {
      const error = new Error(`desktop bridge exited (${event.code ?? "unknown"})`);
      for (const pending of this.pending.values()) pending.reject(error);
      this.pending.clear();
      this.child = null;
    });
    this.child = await command.spawn();
    return this.request("hello");
  }

  onMessage(listener: (message: BridgeMessage) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async request(type: string, payload: Record<string, unknown> = {}): Promise<BridgeMessage> {
    if (!this.child) throw new Error("desktop bridge is not running");
    const requestId = `desktop-${this.nextId++}`;
    const result = new Promise<BridgeMessage>((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
    });
    await this.child.write(
      JSON.stringify({ protocol_version: 1, request_id: requestId, type, payload }) + "\n",
    );
    return result;
  }

  async close(confirm = false): Promise<void> {
    if (!this.child) return;
    try {
      await this.request("shutdown", { confirm });
    } finally {
      this.child = null;
    }
  }

  private receive(line: string): void {
    for (const raw of line.split("\n")) {
      if (!raw.trim()) continue;
      let message: BridgeMessage;
      try {
        message = JSON.parse(raw) as BridgeMessage;
      } catch {
        console.warn("invalid bridge output", raw);
        continue;
      }
      if (message.message_type === "response" && message.request_id) {
        const pending = this.pending.get(message.request_id);
        if (pending) {
          this.pending.delete(message.request_id);
          if (message.ok === false) {
            pending.reject(new Error(message.error?.message ?? "bridge request failed"));
          } else {
            pending.resolve(message);
          }
        }
      }
      for (const listener of this.listeners) listener(message);
    }
  }
}
