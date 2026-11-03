"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { SecretInput } from "@/components/ui/secret-input";

type Connection = { command: string; args?: string[]; cwd?: string; env_from?: Record<string, string> };
type Extension = {
  id: string; name: string; description: string; type: string; source: string; version: string;
  status: string; reasons: string[]; dependencies: string[];
  skills: { name: string; path: string }[]; mcp_servers: Record<string, Connection>;
  dependency_options?: string[];
};
type Chat = { id: string; title: string; pending: boolean; busy: boolean };
const types: Record<string, string> = { skill: "Скилл", mcp: "MCP", plugin: "Плагин" };
const statuses: Record<string, string> = { ready: "Готово", requires_setup: "Требует настройки", error: "Ошибка" };

async function post(body: Record<string, unknown>) {
  const response = await fetch("/api/extensions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось изменить расширения");
  return data;
}

export function ExtensionsSettings({ profileId, engine, selected, onSelectionChange }: {
  profileId?: string; engine?: string; selected?: string[]; onSelectionChange?: (ids: string[]) => void;
}) {
  const [items, setItems] = useState<Extension[]>([]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState("");
  const [type, setType] = useState("skill");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [preview, setPreview] = useState<{ preview: Extension; fingerprint: string } | null>(null);
  const [editing, setEditing] = useState<Extension | null>(null);
  const [configuration, setConfiguration] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const [hasSecrets, setHasSecrets] = useState<Record<string, boolean>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [clearSecrets, setClearSecrets] = useState<string[]>([]);
  const [profileExists, setProfileExists] = useState<boolean | null>(null);
  const [selectionSaving, setSelectionSaving] = useState(false);
  const selectionPending = useRef(false);
  const selectionRef = useRef(selected || []);
  selectionRef.current = selected || [];
  const changeRef = useRef(onSelectionChange);
  changeRef.current = onSelectionChange;
  const requestVersion = useRef(0);
  const mountedRef = useRef(true);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    const query = new URLSearchParams();
    if (profileId) { query.set("profile", profileId); query.set("provider", engine || "api"); }
    const response = await fetch("/api/extensions" + (profileId ? "?" + query.toString() : ""));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось загрузить расширения");
    if (version !== requestVersion.current || !mountedRef.current) return;
    setItems(data.extensions); setChats(data.sessions || []);
    setHasSecrets(data.has_secrets || {});
    setProfileExists(Boolean(data.profile_exists));
    if (profileId && data.profile_exists && !selectionPending.current &&
        JSON.stringify(data.selected) !== JSON.stringify(selectionRef.current)) {
      changeRef.current?.(data.selected);
    }
  }, [profileId, engine]);
  useEffect(() => {
    let mounted = true;
    mountedRef.current = true;
    const refresh = () => { void load().catch((e: Error) => { if (mounted) setError(e.message); }); };
    refresh();
    const timer = window.setInterval(refresh, 1500);
    return () => { mounted = false; mountedRef.current = false; window.clearInterval(timer); requestVersion.current++; };
  }, [load]);

  async function act(task: () => Promise<void>) {
    setBusy(true); setError("");
    try { await task(); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Ошибка расширения"); }
    finally { setBusy(false); }
  }
  async function choose(ids: string[]) {
    if (selectionPending.current) return;
    const next = [...new Set(ids)].sort();
    const previous = selectionRef.current;
    onSelectionChange?.(next);
    if (!profileExists || !profileId) return;
    selectionPending.current = true;
    requestVersion.current++;
    setSelectionSaving(true); setError("");
    try {
      const result = await post({ action: "select", profile_id: profileId, selected: next });
      if (mountedRef.current) onSelectionChange?.(result.selected);
    } catch (e) {
      if (mountedRef.current) {
        onSelectionChange?.(previous);
        setError(e instanceof Error ? e.message : "Не удалось сохранить выбор");
      }
    } finally {
      selectionPending.current = false;
      if (mountedRef.current) {
        setSelectionSaving(false);
        await load().catch((e: Error) => setError(e.message));
      }
    }
  }
  function addDependencies(item: Extension) {
    const result = new Set(selected || []);
    const visit = (entry: Extension) => {
      if (result.has(entry.id)) return;
      result.add(entry.id);
      entry.dependencies.forEach((id) => { const dependency = items.find((candidate) => candidate.id === id); if (dependency) visit(dependency); });
    };
    // Start from each dependency because the parent is normally already selected.
    [...item.dependencies, ...(item.dependency_options || [])].forEach((id) => { const entry = items.find((candidate) => candidate.id === id); if (entry) visit(entry); });
    void choose([...result]);
  }
  function reasons(item: Extension) {
    if (!profileId) return item.reasons;
    const own = item.reasons.filter((reason) => !reason.startsWith("Включите зависимость:") && !reason.startsWith("Зависимость не готова:") && !reason.startsWith("Этот провайдер"));
    if (engine !== "codex") own.push("Этот провайдер пока не поддерживает расширения");
    for (const id of item.dependencies) {
      const dependency = items.find((entry) => entry.id === id);
      if (dependency && !selected?.includes(id)) own.push("Включите зависимость: " + dependency.name);
      else if (dependency && dependency.status !== "ready") own.push("Зависимость не готова: " + dependency.name);
    }
    return [...new Set(own)];
  }
  let references: string[] = [];
  try {
    references = [...new Set(Object.values(JSON.parse(configuration) as Record<string, Connection>)
      .flatMap((connection) => Object.values(connection.env_from || {})))].sort();
  } catch { /* Invalid JSON is reported on save. */ }
  return <section className="space-y-3 rounded-xl border border-white/10 p-3">
    <h3 className="text-sm font-medium">Расширения</h3>
    <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void act(async () => {})}>Обновить состояние</Button>
    {profileId ? <>
      <p className="text-xs text-muted-foreground">{profileExists === false ? "Для нового профиля сначала сохраните настройки. Затем галочки будут сохраняться сразу." : "Галочки сохраняются сразу. Новые скиллы появляются здесь автоматически."}</p>
      {engine !== "codex" && <p className="text-xs text-amber-500">Этот провайдер пока не поддерживает расширения. Выбор сохранится для Codex.</p>}
      <p className="text-xs text-muted-foreground">Расширения доступны со следующего сообщения. Если агент сейчас отвечает, ответ завершится с прежним набором. История сохраняется.</p>
      {selectionSaving && <p role="status" className="text-xs text-muted-foreground">Сохраняю выбор…</p>}
    </> : <p className="text-xs text-muted-foreground">Локальная библиотека скиллов, MCP и плагинов. После добавления выберите расширения в нужном профиле. Установочные команды не выполняются.</p>}
    {error && <p role="alert" className="text-xs text-destructive whitespace-pre-wrap">{error}</p>}
    {!items.length && <p className="text-xs text-muted-foreground">Библиотека пуста. Добавьте локальное расширение в общих настройках.</p>}
    {items.map((item) => <div key={item.id} data-extension-id={item.id} className="space-y-2 rounded-lg border border-white/10 p-3">
      <div className="flex items-start gap-2">
        {profileId && <input aria-label={"Включить " + item.name} type="checkbox" checked={selected?.includes(item.id) || false}
          disabled={selectionSaving || profileExists === null}
          onChange={(event) => void choose(event.target.checked ? [...(selected || []), item.id] : (selected || []).filter((id) => id !== item.id))} />}
        <div className="min-w-0 flex-1"><p className="text-sm font-medium">{item.name}</p><p className="text-xs text-muted-foreground">{item.description}</p></div>
        <span className="text-xs">{types[item.type]} · {statuses[reasons(item).length ? (item.status === "error" ? "error" : "requires_setup") : "ready"]}</span>
      </div>
      {reasons(item).map((reason) => <p key={reason} className="text-xs text-amber-500">{reason}</p>)}
      {profileId && [...item.dependencies, ...(item.dependency_options || [])].some((id) => !selected?.includes(id) && items.some((entry) => entry.id === id)) &&
        <Button type="button" size="sm" variant="outline" disabled={selectionSaving} onClick={() => addDependencies(item)}>Подключить зависимости</Button>}
      <details className="text-xs"><summary className="cursor-pointer">Подробнее</summary>
        <div className="mt-2 space-y-1 break-all text-muted-foreground">
          <p>ID: {item.id}</p><p>Источник: {item.source}</p><p>Версия: {item.version || "не указана"}</p>
          <p>Зависимости: {item.dependencies.join(", ") || "не объявлены"}</p>
          <p>Скиллы: {item.skills.map((skill) => skill.name).join(", ") || "нет"}</p>
          <p>MCP: {Object.keys(item.mcp_servers).join(", ") || "нет"}</p>
          {!!Object.keys(item.mcp_servers).length && <p>Инструменты разных серверов разделены пространствами имён, совпадающие подключения регистрируются один раз.</p>}
          <pre className="whitespace-pre-wrap">{JSON.stringify(item.mcp_servers, null, 2)}</pre>
        </div>
      </details>
      {!profileId && <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => {
          setEditing(item); setEditDescription(item.description); setConfiguration(JSON.stringify(item.mcp_servers, null, 2)); setSecrets({}); setClearSecrets([]);
        }}>Настроить</Button>
        {!!Object.keys(item.mcp_servers).length && <Button type="button" size="sm" variant="outline" disabled={busy}
          onClick={() => void act(async () => { await post({ action: "probe", id: item.id }); })}>Запустить и проверить MCP</Button>}
        {removing === item.id ? <><span className="text-xs">Удалить из библиотеки и всех профилей?</span>
          <Button type="button" size="sm" variant="destructive" disabled={busy} onClick={() => void act(async () => {
            await post({ action: "remove", id: item.id }); setRemoving(null); if (editing?.id === item.id) setEditing(null);
          })}>Удалить</Button><Button type="button" size="sm" variant="ghost" onClick={() => setRemoving(null)}>Отмена</Button></> :
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setRemoving(item.id)}>Удалить</Button>}
      </div>}
    </div>)}
    {profileId && chats.some((chat) => chat.pending) && <div className="space-y-2">
      <p className="text-xs">Новый набор применится автоматически при следующем сообщении:</p>
      {chats.filter((chat) => chat.pending).map((chat) => <div key={chat.id} className="flex items-center justify-between gap-2 text-xs">
        <span>{chat.title}{chat.busy ? " · текущий ответ продолжается" : " · готово к следующему сообщению"}</span>
      </div>)}
    </div>}
    {!profileId && editing && <div className="space-y-2 rounded-lg border border-white/10 p-3">
      <p className="text-sm">Настроить: {editing.name}</p>
      <label className="block text-xs">Короткое описание<Input value={editDescription} maxLength={500} onChange={(e) => setEditDescription(e.target.value)} /></label>
      {!!Object.keys(editing.mcp_servers).length && <>
        <label className="block text-xs">Параметры stdio MCP (JSON)<Textarea className="min-h-40 font-mono text-xs" value={configuration} onChange={(e) => setConfiguration(e.target.value)} /></label>
        <p className="text-xs text-muted-foreground">command — полный путь к установленной программе; args — массив аргументов; cwd — рабочая папка; env_from — имена переменных MCP и соответствующих секретов или переменных окружения kid. Ключи и токены задайте ниже. Они сохраняются существующим механизмом настроек секретов kid. Проверка запускает указанные программы.</p>
        {references.map((reference) => <div key={reference} className="space-y-1">
          <SecretInput label={reference} value={secrets[reference] || ""} saved={hasSecrets[reference] && !clearSecrets.includes(reference)}
            cleared={clearSecrets.includes(reference)} onChange={(value) => { setSecrets({ ...secrets, [reference]: value }); setClearSecrets(clearSecrets.filter((key) => key !== reference)); }} />
          <Button type="button" size="sm" variant="ghost" onClick={() => { setClearSecrets([...clearSecrets, reference]); setSecrets({ ...secrets, [reference]: "" }); }}>Удалить сохранённый секрет</Button>
        </div>)}
      </>}
      <Button type="button" size="sm" disabled={busy} onClick={() => void act(async () => {
        await post({ action: "configure", id: editing.id, description: editDescription, mcp_servers: JSON.parse(configuration),
          secrets: Object.fromEntries(Object.entries(secrets).filter(([key]) => references.includes(key))), clear_secrets: clearSecrets.filter((key) => references.includes(key)) }); setEditing(null);
      })}>Сохранить настройку</Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}>Отмена</Button>
    </div>}
    {!profileId && <div className="space-y-2 border-t border-white/10 pt-3">
      <p className="text-sm">Добавить из локального источника</p>
      <label className="block text-xs">Тип<select className="ml-2 rounded border bg-background p-1" value={type} onChange={(e) => { setType(e.target.value); setPreview(null); }}>
        {Object.entries(types).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label className="block text-xs">Источник<Input placeholder="Полный путь к SKILL.md, mcp.json или kid-extension.json" value={source} onChange={(e) => { setSource(e.target.value); setPreview(null); }} /></label>
      <label className="block text-xs">Название (если отсутствует в источнике)<Input value={name} maxLength={120} onChange={(e) => { setName(e.target.value); setPreview(null); }} /></label>
      <label className="block text-xs">Короткое описание (до 500 символов)<Input placeholder="Например: чтение и обработка PDF" value={description} maxLength={500} onChange={(e) => { setDescription(e.target.value); setPreview(null); }} /></label>
      <details className="text-xs text-muted-foreground"><summary>Поддерживаемые форматы</summary>
        <p>Скилл: папка с SKILL.md и ресурсами (до 20 МБ). MCP: локальный JSON с name, description и mcp_servers. Плагин: kid-extension.json с format="kid-extension-v1", name, description, skills (относительные пути), mcp_servers и необязательными dependencies (ID библиотеки).</p>
        <pre className="whitespace-pre-wrap">{JSON.stringify({ name: "Мой MCP", description: "Назначение для пользователя", mcp_servers: { server: { command: "C:/tools/server.exe", args: [], env_from: { API_KEY: "MY_MCP_API_KEY" } } } }, null, 2)}</pre>
        <p>HTTP/SSE MCP, установочные сценарии, GitHub и произвольные форматы плагинов в этой версии не поддерживаются. Расширения могут выполнять локальный код и использовать обычную сеть компьютера.</p>
      </details>
      <Button type="button" size="sm" variant="outline" disabled={busy || !source.trim()} onClick={() => void act(async () => {
        setPreview(await post({ action: "preview", source, type, name, description }));
      })}>Проверить источник и состав</Button>
      {preview && <div className="space-y-2 rounded border border-white/10 p-2 text-xs">
        <p>{preview.preview.name} · {types[type]} · {preview.preview.version || "версия не указана"}</p>
        <p>{preview.preview.description}</p><p className="break-all">Источник: {preview.preview.source}</p>
        <p>Скиллы: {preview.preview.skills.map((skill) => skill.name).join(", ") || "нет"}</p>
        <p>MCP: {Object.keys(preview.preview.mcp_servers).join(", ") || "нет"}</p>
        <pre className="whitespace-pre-wrap break-all">{JSON.stringify(preview.preview.mcp_servers, null, 2)}</pre>
        <p>Зависимости: {preview.preview.dependencies.join(", ") || "не объявлены"}</p>
        <Button type="button" size="sm" disabled={busy} onClick={() => void act(async () => {
          await post({ action: "install", source, type, name, description, fingerprint: preview.fingerprint });
          setPreview(null); setSource(""); setName(""); setDescription("");
        })}>Добавить в библиотеку</Button>
      </div>}
    </div>}
  </section>;
}
