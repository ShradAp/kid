/** One worker per chat; a failed/uncertain turn pauses its remaining requests. */
export class ChatRequestQueue<T> {
  private pending = new Map<string, T[]>();
  private busy = new Set<string>();
  private paused = new Set<string>();
  constructor(
    private run: (sessionId: string, request: T) => Promise<boolean>,
    private changed: () => void,
  ) {}
  isBusy(sessionId: string) { return this.busy.has(sessionId); }
  running() { return [...this.busy]; }
  items(sessionId: string): readonly T[] { return this.pending.get(sessionId) ?? []; }
  enqueue(sessionId: string, request: T) {
    this.pending.set(sessionId, [...this.items(sessionId), request]);
    this.changed();
    if (!this.paused.has(sessionId)) this.start(sessionId);
  }
  remove(sessionId: string, index: number) {
    this.pending.set(sessionId, this.items(sessionId).filter((_, i) => i !== index));
    if (!this.items(sessionId).length) this.paused.delete(sessionId);
    this.changed();
  }
  clear(sessionId: string) { this.pending.delete(sessionId); this.paused.delete(sessionId); this.changed(); }
  start(sessionId: string) {
    if (this.isBusy(sessionId)) return;
    this.paused.delete(sessionId);
    const request = this.pending.get(sessionId)?.shift();
    if (request === undefined) return;
    this.busy.add(sessionId);
    this.changed();
    void this.run(sessionId, request).catch(() => false).then((completed) => {
      this.busy.delete(sessionId);
      if (!completed && this.items(sessionId).length) this.paused.add(sessionId);
      this.changed();
      if (completed) this.start(sessionId);
    });
  }
}
