"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";

type Release = {
  current?: string | null;
  latest?: string;
  available?: boolean;
  can_install?: boolean;
  message?: string;
  error?: string;
};

export function UpdateSettings() {
  const [release, setRelease] = useState<Release | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState("");
  const [started, setStarted] = useState(false);

  async function check() {
    setBusy(true);
    setConfirm(false);
    setStarted(false);
    setRelease(null);
    setMessage("");
    try {
      const response = await fetch("/api/updates");
      const data: Release = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось проверить обновления.");
      setRelease(data);
      setMessage(data.message || "");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Ошибка проверки обновлений.");
    } finally { setBusy(false); }
  }

  async function install() {
    setBusy(true);
    try {
      const response = await fetch("/api/updates/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: release?.latest, confirm: true }),
      });
      const data: Release = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось открыть установщик.");
      setStarted(true);
      setConfirm(false);
      setMessage(data.message || "Установщик открыт.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Ошибка обновления.");
    } finally { setBusy(false); }
  }

  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted-foreground">Проверка стабильных версий на GitHub. Новая версия устанавливается отдельно; переключение происходит после успешной установки.</p>
      {release?.current && <p>Текущая версия: {release.current}</p>}
      {release?.latest && <p>Последняя версия: {release.latest}</p>}
      <Button variant="outline" disabled={busy} onClick={() => void check()}>
        {busy ? "Подождите…" : "Проверить обновления"}
      </Button>
      {release?.available && release.can_install && !started && !confirm && (
        <Button className="ml-2" disabled={busy} onClick={() => setConfirm(true)}>Обновить до {release.latest}</Button>
      )}
      {confirm && (
        <div className="space-y-2 rounded-lg border border-white/10 p-3">
          <p>Скачать и установить {release?.latest}? После завершения нужно закрыть kid через трей и снова открыть ярлык на рабочем столе. Настройки и история сохранятся.</p>
          <Button disabled={busy} onClick={() => void install()}>Подтвердить установку</Button>
          <Button className="ml-2" variant="outline" disabled={busy} onClick={() => setConfirm(false)}>Отмена</Button>
        </div>
      )}
      {message && <p role="status" className="text-xs text-muted-foreground">{message}</p>}
    </div>
  );
}
