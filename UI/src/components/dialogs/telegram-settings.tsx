"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SecretInput } from "@/components/ui/secret-input";
import { Switch } from "@/components/ui/switch";
import { Field, FieldLabel, FieldDescription } from "@/components/ui/field";

type Settings = {
  live_status: boolean; streaming: boolean; notifications: boolean;
  enabled: boolean; has_token: boolean; owner_id: string; profile_id: string;
  network: string; profiles: { id: string; name: string }[];
  running: boolean; busy: boolean; queued: number; pending_delivery: number;
  messages: number; error: string; preview_error?: string; last_contact: number | null;
  archive_count: number;
  last_turn?: { queue_ms: number; preparation_ms?: number; codex_ms: number; first_text_ms: number | null; delivery_ms: number | null; total_ms?: number } | null;
};

export function TelegramSettings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [token, setToken] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [live, setLive] = useState<Settings | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function load(initial = false) {
      try {
        const response = await fetch("/api/telegram/settings", { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Не удалось загрузить Telegram");
        if (!controller.signal.aborted) {
          setLive(data);
          if (initial) setSettings(data);
        }
      } catch (error) {
        if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Telegram недоступен");
      }
    }
    void load(true);
    const timer = window.setInterval(() => void load(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);

  async function submit(action: "settings" | "clear" | "stop" | "panel" | "archive/clear") {
    if (!settings || busy) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/telegram/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "settings" ? {
          enabled: settings.enabled, owner_id: settings.owner_id, profile_id: settings.profile_id,
          network: settings.network, token, clear_token: clearToken,
          live_status: settings.live_status, streaming: settings.streaming, notifications: settings.notifications,
        } : action === "archive/clear" ? { confirm: true } : {}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Не удалось выполнить действие");
      setLive(data);
      if (action === "settings") { setSettings(data); setToken(""); setClearToken(false); }
      setConfirmClear(false);
      setConfirmArchive(false);
      setMessage(action === "archive/clear" ? "Архив входящих файлов удалён, контекст сброшен." : action === "panel" ? "Панель открывается в Telegram." : action === "clear" ? "Переписка очищена, контекст сброшен." : action === "stop" ? "Запрошена остановка, очередь очищена." : "Настройки сохранены.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Telegram недоступен");
    } finally { setBusy(false); }
  }

  if (!settings) return <p role="status" className="text-xs text-muted-foreground">{message || "Загрузка настроек Telegram…"}</p>;
  const selectClass = "h-9 w-full rounded-md border border-input bg-background px-2 text-sm";
  return <div className="flex flex-col gap-3">
    <Field>
      <div className="flex items-center justify-between gap-3">
        <FieldLabel htmlFor="telegram-enabled">Telegram-бот</FieldLabel>
        <Switch id="telegram-enabled" checked={settings.enabled} onCheckedChange={(enabled) => setSettings({ ...settings, enabled })} />
      </div>
      <FieldDescription>Один отдельный чат. KID должен работать на компьютере; его можно свернуть в трей. Закрытие окна завершает приложение.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="telegram-token">Токен от BotFather</FieldLabel>
      <SecretInput id="telegram-token" label="Токен Telegram" value={token} saved={settings.has_token} cleared={clearToken}
        onChange={(value) => { setToken(value); setClearToken(false); }} />
      <FieldDescription>{clearToken ? "Токен будет удалён при сохранении." : settings.has_token ? "Токен сохранён. Оставьте поле пустым, чтобы сохранить его." : "Создайте бота в @BotFather и вставьте токен здесь."}</FieldDescription>
      {settings.has_token && <Button size="sm" variant="outline" type="button" onClick={() => { setToken(""); setClearToken(!clearToken); }}>
        {clearToken ? "Отменить удаление токена" : "Удалить сохранённый токен"}
      </Button>}
    </Field>
    <Field>
      <FieldLabel htmlFor="telegram-owner">Ваш числовой Telegram ID</FieldLabel>
      <Input id="telegram-owner" inputMode="numeric" value={settings.owner_id}
        onChange={(event) => setSettings({ ...settings, owner_id: event.target.value })} placeholder="Например, 123456789" />
      <FieldDescription>Принимаются только личные сообщения этого пользователя.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="telegram-profile">Профиль Codex</FieldLabel>
      <select id="telegram-profile" className={selectClass} value={settings.profile_id}
        onChange={(event) => setSettings({ ...settings, profile_id: event.target.value })}>
        <option value="">Выберите профиль</option>
        {settings.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
      </select>
      <FieldDescription>Используются модель, рабочая папка и разрешения выбранного профиля. Вход в Codex выполняется на ПК. Перед сменой профиля очистите переписку Telegram.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="telegram-network">Подключение к Telegram</FieldLabel>
      <select id="telegram-network" className={selectClass} value={settings.network}
        onChange={(event) => setSettings({ ...settings, network: event.target.value })}>
        <option value="direct">Напрямую с компьютера</option>
        <option value="proxy">Через общий прокси KID</option>
      </select>
      <FieldDescription>Соединение Codex всегда использует его штатный защищённый прокси.</FieldDescription>
    </Field>
    {([
      ["live_status", "Живой статус запроса"],
      ["streaming", "Постепенное появление ответа"],
      ["notifications", "Уведомления о запуске и восстановлении связи"],
    ] as const).map(([key, label]) => <Field key={key}>
      <div className="flex items-center justify-between gap-3">
        <FieldLabel htmlFor={`telegram-${key}`}>{label}</FieldLabel>
        <Switch id={`telegram-${key}`} checked={settings[key]} onCheckedChange={(value) => setSettings({ ...settings, [key]: value })} />
      </div>
    </Field>)}
    <FieldDescription>Предпросмотр обновляется примерно раз в 1,5 секунды. Готовый ответ приходит отдельным сообщением с оформлением. /panel открывает обновляемую панель с кнопками.</FieldDescription>
    <Button disabled={busy} onClick={() => void submit("settings")}>{busy ? "Подождите…" : "Сохранить настройки Telegram"}</Button>
    <div role="status" className="text-xs text-muted-foreground">
      {live?.running ? "Модуль работает" : "Модуль остановлен"} · {live?.busy ? "Codex выполняет запрос" : "Нет активного запроса"}
      <br />В очереди: {live?.queued ?? 0} · Ожидают доставки: {live?.pending_delivery ?? 0} · Сообщений: {live?.messages ?? 0}
      <br />Файлов в архиве: {live?.archive_count ?? 0}. Очистка чата сохраняет вложения для поиска в следующих разговорах.
      {live?.last_contact && <><br />Последняя связь: {new Date(live.last_contact * 1000).toLocaleTimeString()}</>}
      {live?.error && <p className="mt-1 text-destructive">{live.error}</p>}
      {live?.preview_error && <p className="mt-1">{live.preview_error}</p>}
      {live?.last_turn && <p className="mt-1">
        Последний запрос: очередь {(live.last_turn.queue_ms / 1000).toFixed(1)} с · Codex {(live.last_turn.codex_ms / 1000).toFixed(1)} с
        {!!live.last_turn.preparation_ms && <> · подготовка {(live.last_turn.preparation_ms / 1000).toFixed(1)} с</>}
        {live.last_turn.delivery_ms != null && <> · доставка {(live.last_turn.delivery_ms / 1000).toFixed(1)} с</>}
        {live.last_turn.first_text_ms != null && <><br />Первый текст от Codex: {(live.last_turn.first_text_ms / 1000).toFixed(1)} с</>}
      </p>}
    </div>
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" disabled={busy || !live?.running} onClick={() => void submit("panel")}>Открыть /panel в Telegram</Button>
      <Button variant="outline" size="sm" disabled={busy} onClick={() => void submit("stop")}>Остановить запрос</Button>
      <Button variant="outline" size="sm" disabled={busy} onClick={() => setConfirmClear(!confirmClear)}>Очистить переписку</Button>
      <Button variant="outline" size="sm" disabled={busy || live?.busy || !!live?.queued || !!live?.pending_delivery} onClick={() => setConfirmArchive(!confirmArchive)}>Удалить архив входящих файлов</Button>
    </div>
    {confirmClear && <div className="rounded-md border border-destructive/30 p-2 text-xs">
      История, очередь и ожидающие ответы Telegram будут удалены, текущий запрос остановлен.
      <Button variant="destructive" size="sm" className="mt-2 w-full" disabled={busy} onClick={() => void submit("clear")}>Подтвердить очистку</Button>
    </div>}
    {confirmArchive && <div className="rounded-md border border-destructive/30 p-2 text-xs">
      Сохранённые входящие файлы этого бота и владельца будут удалены с ПК, контекст KID сброшен. Сообщения и вложения в Telegram останутся.
      <Button variant="destructive" size="sm" className="mt-2 w-full" disabled={busy} onClick={() => void submit("archive/clear")}>Подтвердить удаление архива</Button>
    </div>}
    <p className="text-xs text-muted-foreground">Команды бота: /help, /panel, /files, /status, /stop, /clear. Текст, документы и фото: приём до 20 МБ, отправка файлов до 50 МБ. Настройки, переписка и файлы сохраняются локально рядом с конфигом KID.</p>
    {message && <p role="status" className="text-xs text-muted-foreground">{message}</p>}
  </div>;
}
