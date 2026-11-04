"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ChevronDown, ShieldCheck, ShieldAlert } from "lucide-react";

interface QuotaWindow {
  bucket: string;
  slot: string;
  duration_minutes: number | null;
  remaining_percent: number | null;
  resets_at: number | null;
}

interface CodexStatus {
  connecting?: boolean;
  available: boolean;
  proxy_state: string;
  auth_state: string;
  error: string;
  reason: string;
  exit_ip?: string | null;
  login?: { verification_url: string; user_code: string; login_id: string } | null;
  models: Array<{ id: string; model: string; displayName: string; supportedReasoningEfforts: Array<{ reasoningEffort: string }> }>;
  limits: { windows: QuotaWindow[]; updated_at: number | null; stale: boolean };
  capabilities: { login: boolean; chat: boolean };
}

export function useCodexStatus(profileId: string) {
  const [snapshot, setSnapshot] = useState<{ id: string; data: CodexStatus } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null);
  const refresh = useCallback(async (action = "status", signal?: AbortSignal) => {
    setBusy(true);
    try {
      const response = await fetch(action === "status"
        ? `/api/codex/status?profile_id=${encodeURIComponent(profileId)}`
        : `/api/codex/${action}`, action === "status" ? { signal } : {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile_id: profileId }), signal,
        });
      const data = await response.json();
      // A blocked runtime returns a useful status payload with HTTP 503.
      if (!data.limits || !data.capabilities) throw new Error(data.error || "Состояние Codex недоступно");
      if (!signal?.aborted) {
        setSnapshot({ id: profileId, data });
        setFailure(null);
      }
    } catch (error) {
      if (!signal?.aborted) setFailure({ id: profileId, message: error instanceof Error ? error.message : "Нет связи с приложением" });
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }, [profileId]);
  useEffect(() => {
    const controller = new AbortController();
    void refresh("status", controller.signal);
    const timer = window.setInterval(() => void refresh("refresh", controller.signal), 60_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function readLocalLimits() {
      if (pending) return;
      pending = true;
      try {
        const response = await fetch(`/api/codex/limits?profile_id=${encodeURIComponent(profileId)}`, { signal: controller.signal });
        if (!response.ok) throw new Error("Лимиты недоступны");
        const result = await response.json();
        if (!result.limits || !Array.isArray(result.limits.windows)) throw new Error("Лимиты недоступны");
        if (!controller.signal.aborted) setSnapshot((current) => current?.id === profileId
          ? { ...current, data: { ...current.data, limits: result.limits } } : current);
      } catch {
        if (!controller.signal.aborted) setSnapshot((current) => current?.id === profileId
          ? { ...current, data: { ...current.data, limits: { ...current.data.limits, stale: true } } } : current);
      } finally { pending = false; }
    }
    const timer = window.setInterval(() => void readLocalLimits(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [profileId]);
  const loginId = snapshot?.id === profileId ? snapshot.data.login?.login_id : undefined;
  const awaitingConnection = snapshot?.id === profileId && snapshot.data.available && snapshot.data.proxy_state === "not_checked";
  useEffect(() => {
    if (!loginId && !awaitingConnection) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void refresh("status", controller.signal), 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [loginId, awaitingConnection, refresh]);
  return { data: snapshot?.id === profileId ? snapshot.data : null, busy,
    failure: failure?.id === profileId ? failure.message : null, refresh };
}

function resetText(timestamp: number | null) {
  if (timestamp === null) return "сброс неизвестен";
  const minutes = Math.max(0, Math.ceil((timestamp * 1000 - Date.now()) / 60_000));
  return `сброс через ${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

export function CodexQuota({ profileId }: { profileId: string }) {
  const { data, busy, failure, refresh } = useCodexStatus(profileId);
  const windows = data?.limits.windows ?? [];
  const verified = data?.proxy_state === "verified";
  const stale = Boolean(failure || data?.limits.stale);
  const slots = [{ duration: 300, label: "5 ч" }, { duration: 10080, label: "Неделя" }];
  const percent = (item: QuotaWindow) => item.remaining_percent === null ? "—" : `${item.remaining_percent}%`;
  return <Popover>
    <PopoverTrigger render={<Button variant="ghost" size="sm" className="gap-2 rounded-full border border-white/10 bg-white/[0.04] text-xs" />} aria-label="Лимиты и состояние Codex">
      {verified ? <ShieldCheck className="size-3.5 text-muted-foreground" /> : <ShieldAlert className="size-3.5 text-amber-400" />}
      <span>Codex</span>
      {slots.map(({ duration, label }) => <span key={duration} className="text-muted-foreground">{label} {windows.filter(item => item.duration_minutes === duration).map(percent).join(" / ") || "—"}</span>)}
      {stale && <span className="size-1.5 rounded-full bg-amber-400" aria-label="Данные устарели" />}
      <ChevronDown className="size-3 text-muted-foreground" />
    </PopoverTrigger>
    <PopoverContent align="center" className="w-80 space-y-2 p-4">
      <p className="font-medium">{verified ? "Прокси: защищённый канал" : data?.connecting ? "Автоматическое подключение к прокси…" : data?.proxy_state === "blocked" ? "Запросы Codex заблокированы" : "Подключение к прокси запускается автоматически"}</p>
      {slots.map(({ duration, label }) => <div key={duration} className="space-y-1 text-xs">
        <p className="font-medium">{label}</p>
        {windows.filter(item => item.duration_minutes === duration).map((item, index) => <p key={`${item.bucket}-${index}`} className="text-muted-foreground">{item.bucket}: {percent(item)} · {resetText(item.resets_at)}</p>)}
        {!windows.some(item => item.duration_minutes === duration) && <p className="text-muted-foreground">Нет данных</p>}
      </div>)}
      {stale && <p className="text-xs text-muted-foreground">Данные устарели</p>}
      {failure && <p className="text-xs text-destructive" role="alert">{failure}</p>}
      <Button size="sm" variant="outline" disabled={busy} onClick={() => void refresh("refresh")}>Обновить лимиты</Button>
    </PopoverContent>
  </Popover>;
}
