"use client";

import { useEffect, useRef, useState } from "react";
import { FolderCode, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";

async function api(path: string, profileId: string, body?: object) {
  const response = await fetch(body ? path : `${path}${path.includes("?") ? "&" : "?"}profile_id=${encodeURIComponent(profileId)}`,
    body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile_id: profileId, ...body }) } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Просмотр недоступен");
  return data;
}

export function CodePanel({ open, onClose, profileId, sessionId, onRecover }: { open: boolean; onClose: () => void; profileId: string; sessionId: string; onRecover: () => void }) {
  const [root, setRoot] = useState("");
  const [files, setFiles] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [text, setText] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<Array<{ method: string; type: string; text: string }>>([]);
  const version = useRef(0);

  async function loadFiles() {
    const data = await api("/api/code/files", profileId);
    setRoot(data.root); setFiles(data.files);
    setNotice(data.truncated ? "Показаны первые 500 файлов, обход ограничен 1000 папками." : "");
  }
  useEffect(() => {
    if (!open) return;
    let current = true;
    setRoot(""); setFiles([]); setSelected(""); setText(""); setError("");
    api("/api/code/files", profileId).then((data) => {
      if (current) { setRoot(data.root); setFiles(data.files); setNotice(data.truncated ? "Список файлов ограничен." : ""); }
    }).catch(() => { if (current) setNotice("Выберите папку. После перезапуска доступ нужно подтвердить повторным выбором."); });
    return () => { current = false; version.current += 1; };
  }, [open, profileId]);
  useEffect(() => {
    if (!open) return;
    let current = true;
    setEvents([]);
    const poll = async () => {
      try {
        const data = await api(`/api/code/events?session_id=${encodeURIComponent(sessionId)}`, profileId);
        if (current) setEvents(data.events ?? []);
      } catch { /* retain the last real events */ }
    };
    void poll();
    const timer = window.setInterval(poll, 1500);
    return () => { current = false; window.clearInterval(timer); };
  }, [open, profileId, sessionId]);

  async function action(work: () => Promise<void>) {
    setBusy(true); setError("");
    try { await work(); } catch (exception) { setError(exception instanceof Error ? exception.message : "Просмотр недоступен"); }
    finally { setBusy(false); }
  }
  async function preview(path: string, diff = false) {
    const request = ++version.current;
    setSelected(path);
    const data = await api(`/api/code/${diff ? "diff" : "preview"}?path=${encodeURIComponent(path)}`, profileId);
    if (request !== version.current) return;
    setText(data.text);
    setNotice([data.description, data.truncated ? "Показаны первые 512 КиБ." : ""].filter(Boolean).join(" "));
  }

  return <Sheet open={open} onOpenChange={(value) => { if (!value) onClose(); }}>
    <SheetContent side="right" showCloseButton={false} className="flex flex-col gap-0 border-white/10 bg-sidebar p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-3xl">
      <SheetHeader className="flex h-16 shrink-0 flex-row items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
        <SheetTitle className="flex items-center gap-2 text-sm font-semibold text-foreground"><FolderCode className="size-4 text-primary" />Проект · лёгкий просмотр</SheetTitle>
        <button type="button" aria-label="Закрыть проект" onClick={onClose} className="rounded-full p-1 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"><X className="size-4" /></button>
      </SheetHeader>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <SheetDescription>Локальные файлы, diff и реальные события App Server. Команды Codex выполняются внутри изолированного контейнера.</SheetDescription>
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy} onClick={() => void action(async () => {
            const data = await api("/api/code/select", profileId, {});
            if (data.path) { setSelected(""); setText(""); await loadFiles(); }
          })}>Выбрать папку</Button>
          <Button variant="outline" disabled={!root || busy} onClick={() => void action(async () => { await api("/api/code/open-vscode", profileId, {}); })}>Открыть в VS Code</Button>
          <Button variant="ghost" disabled={!root || busy} onClick={() => void action(loadFiles)}>Обновить</Button>
          <Button variant="outline" disabled={busy} onClick={() => void action(async () => {
            await api("/api/codex/recover", profileId, { session_id: sessionId });
            onRecover();
          })}>Восстановить предыдущий ход</Button>
        </div>
        {events.length > 0 && <section className="space-y-2" aria-label="Действия Codex">
          <h3 className="text-sm font-medium">Действия в этом диалоге</h3>
          {events.map((event, index) => <pre key={index} className="max-h-60 overflow-auto whitespace-pre-wrap rounded-xl border border-white/10 bg-black/40 p-3 font-mono text-xs leading-relaxed text-foreground">{event.text}</pre>)}
        </section>}
        {root && <p className="text-xs break-all text-muted-foreground">{root}</p>}
        <p className="text-xs text-muted-foreground">VS Code откроет отдельное окно; чат расширения Codex имеет собственную сессию.</p>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        {notice && <p className="text-xs text-muted-foreground">{notice}</p>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <nav aria-label="Файлы проекта" className="max-h-60 overflow-auto rounded-xl border border-white/10 bg-white/[0.02] p-2 sm:max-h-96">
            {files.map((path) => <Button key={path} variant="ghost" size="sm" disabled={busy} className={`h-auto w-full justify-start whitespace-normal break-all rounded-lg px-2 py-1.5 text-left text-xs ${selected === path ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"}`}
              onClick={() => void action(() => preview(path))}>{path}</Button>)}
          </nav>
          <div className="min-w-0 space-y-2 sm:col-span-2">
            {selected && <div className="flex gap-2">
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void action(() => preview(selected))}>Текст</Button>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void action(() => preview(selected, true))}>Diff к HEAD</Button>
            </div>}
            <pre className="max-h-96 overflow-auto whitespace-pre rounded-xl border border-white/10 bg-black/40 p-3 font-mono text-xs leading-relaxed text-foreground select-text">{text || "Выберите файл"}</pre>
          </div>
        </div>
      </div>
    </SheetContent>
  </Sheet>;
}
