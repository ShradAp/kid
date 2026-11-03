"use client";

import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Mic, MicOff } from "lucide-react";

type VoiceState = {
  enabled: boolean;
  state: "off" | "starting" | "waiting" | "recording";
  error: string;
  partial: string;
  sequence: number;
  events: { id: number; text: string }[];
  other_session?: boolean;
};

const OFF: VoiceState = {
  enabled: false, state: "off", error: "", partial: "", sequence: 0, events: [],
};

export function VoiceControl({
  sessionId,
  onText,
}: {
  sessionId: string;
  onText: (text: string) => void;
}) {
  const [voice, setVoice] = useState<VoiceState>(OFF);
  const [busy, setBusy] = useState(false);
  const alive = useRef(false);
  const sequence = useRef(0);
  const revision = useRef(0);
  const toggling = useRef(false);
  const enabled = useRef(false);
  const initialized = useRef(false);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  function apply(data: VoiceState, deliver = true) {
    if (!alive.current) return;
    if (deliver) {
      for (const event of data.events ?? []) {
        if (event.id > sequence.current) {
          onTextRef.current(event.text);
          sequence.current = event.id;
        }
      }
    }
    sequence.current = Math.max(sequence.current, data.sequence ?? 0);
    enabled.current = data.enabled;
    setVoice(data);
  }

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();

    async function poll() {
      const currentRevision = revision.current;
      try {
        if (!toggling.current) {
          const params = new URLSearchParams({
            session_id: sessionId, after: String(sequence.current),
          });
          const response = await fetch("/api/voice-input?" + params, { signal: abort.signal });
          if (!response.ok) throw new Error("Не удалось получить состояние микрофона");
          const data: VoiceState = await response.json();
          if (!cancelled && currentRevision === revision.current) {
            apply(data, initialized.current);
            initialized.current = true;
          }
        }
      } catch (error) {
        if (!cancelled) {
          setVoice((previous) => ({
            ...previous,
            error: error instanceof Error ? error.message : "Диктовка недоступна",
          }));
        }
      } finally {
        if (!cancelled) timer = setTimeout(poll, enabled.current ? 500 : 2500);
      }
    }
    void poll();
    return () => {
      cancelled = true;
      alive.current = false;
      clearTimeout(timer);
      abort.abort();
      // Session ownership prevents a departing chat from stopping another chat.
      void fetch("/api/voice-input", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, enabled: false, after: sequence.current }),
        keepalive: true,
      }).catch(() => {});
    };
    // This component is keyed by sessionId; refs and cursors belong to one draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  async function toggle() {
    if (toggling.current) return;
    toggling.current = true;
    revision.current += 1;
    setBusy(true);
    const turnOn = !enabled.current;
    try {
      const response = await fetch("/api/voice-input", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId, enabled: turnOn, after: sequence.current,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось переключить микрофон");
      if (alive.current) {
        initialized.current = true;
        apply(data);
      } else if (turnOn) {
        // An enable request can finish after the user has switched chats.
        await fetch("/api/voice-input", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, enabled: false }),
          keepalive: true,
        });
      }
    } catch (error) {
      if (alive.current) setVoice((previous) => ({
        ...previous, error: error instanceof Error ? error.message : "Диктовка недоступна",
      }));
    } finally {
      toggling.current = false;
      if (alive.current) setBusy(false);
    }
  }

  const label = voice.state === "recording"
    ? "Записываю. Скажи «конец записи», чтобы остановить диктовку."
    : voice.state === "starting"
      ? "Включаю микрофон…"
      : "Жду «начать запись». Микрофон слушает локально.";
  return (
    <div className="relative flex shrink-0 items-center">
      <button
        type="button"
        onClick={() => void toggle()}
        disabled={busy}
        aria-pressed={voice.enabled}
        aria-label={voice.enabled ? "Выключить фоновое прослушивание" : "Включить фоновое прослушивание"}
        title={voice.enabled ? label : "Голосовой ввод: «начать запись» / «конец записи»"}
        className={
          "inline-flex size-8 items-center justify-center rounded-full transition-colors disabled:opacity-60 " +
          (voice.state === "recording"
            ? "bg-red-500/20 text-red-400"
            : voice.enabled
              ? "bg-emerald-500/15 text-emerald-400"
              : "text-muted-foreground hover:bg-white/10 hover:text-foreground")
        }
      >
        {busy || voice.state === "starting"
          ? <LoaderCircle className="size-4 animate-spin" />
          : voice.enabled ? <Mic className="size-4" /> : <MicOff className="size-4" />}
      </button>
      {(voice.enabled || voice.error) && (
        <div
          role="status"
          className="absolute bottom-12 right-0 z-30 w-72 rounded-xl border border-white/10 bg-card p-3 text-xs text-foreground shadow-xl"
        >
          <p className={voice.error ? "text-red-400" : ""}>{voice.error || label}</p>
          {voice.partial && <p className="mt-2 max-h-20 overflow-hidden text-muted-foreground">{voice.partial}</p>}
          {voice.enabled && <p className="mt-2 text-muted-foreground">Отправка вручную. Кнопка микрофона полностью выключает прослушивание.</p>}
        </div>
      )}
    </div>
  );
}
