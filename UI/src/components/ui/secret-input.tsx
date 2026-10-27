"use client";

import { useEffect, useRef, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { InputGroup, InputGroupInput, InputGroupAddon, InputGroupButton } from "@/components/ui/input-group";

export function SecretInput({ id, label, value, saved = false, cleared = false, onChange, reveal }: {
  id?: string; label: string; value: string; saved?: boolean; onChange: (value: string) => void;
  reveal?: { field: string; profile_id?: string };
  cleared?: boolean;
}) {
  const [shown, setShown] = useState(false);
  const [stored, setStored] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => { if (cleared) { setShown(false); setStored(""); } }, [cleared]);

  async function toggle() {
    setError("");
    if (shown) { setShown(false); setStored(""); return; }
    if (value || !saved || !reveal) { setShown(true); return; }
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    try {
      const response = await fetch("/api/settings/reveal-secret", { method:"POST", signal:controller.signal,
        headers:{"Content-Type":"application/json"}, body:JSON.stringify(reveal) });
      if (!response.ok) throw new Error();
      const result = await response.json();
      if (!controller.signal.aborted) { setStored(result.secret || ""); setShown(true); }
    } catch {
      if (!controller.signal.aborted) setError("Не удалось показать сохранённое значение");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <>
    <InputGroup className="relative min-w-0">
      <InputGroupInput id={id} aria-label={label} type={shown ? "text" : "password"}
        autoComplete="new-password" spellCheck={false} className="font-mono"
        style={!shown && value ? { color:"transparent", caretColor:"var(--foreground)" } : undefined}
        value={shown && !value ? stored : value} placeholder="********"
        onScroll={(event) => setOffset(event.currentTarget.scrollLeft)}
        onChange={(event) => { setStored(""); onChange(event.target.value); }} />
      {!shown && value && <span aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-2.5 right-10 flex select-none items-center overflow-hidden whitespace-pre font-mono text-sm">
        <span style={{ transform:`translateX(-${offset}px)` }}>{"*".repeat(value.length)}</span>
      </span>}
      <InputGroupAddon align="inline-end">
        <InputGroupButton type="button" size="icon-xs" disabled={busy} aria-pressed={shown}
          aria-label={`${shown ? "Скрыть" : "Показать"} ${label.toLowerCase()}`} onClick={() => void toggle()}>
          {shown ? <EyeOff /> : <Eye />}
        </InputGroupButton>
      </InputGroupAddon>
    </InputGroup>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
  </>;
}
