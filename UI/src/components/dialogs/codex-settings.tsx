"use client";

import type { CodexSettings as Settings } from "@/lib/assistant-data";
import { useCodexStatus } from "@/components/widgets/codex-status";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { SecretInput } from "@/components/ui/secret-input";
import { Switch } from "@/components/ui/switch";
import { Field, FieldLabel, FieldDescription, FieldGroup } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export const defaultCodexSettings: Settings = {
  model: "", effort: "", search: "codex", proxy: "", workspace: "",
};

function proxyFields(value: string) {
  const match = /^(https?|socks5h):\/\/(?:([^@]*)@)?(\[[^\]]*\]|[^:]*)(?::(.*))?$/.exec(value);
  if (!match) return { protocol: "http", address: value, port: "", login: "" };
  let login = match[2] ?? "";
  try { login = decodeURIComponent(login); } catch { /* Keep an incomplete edit. */ }
  return { protocol: match[1], address: match[3].replace(/^\[|\]$/g, ""), port: match[4] ?? "", login };
}

export function CodexSettings({ profileId, value, onChange }: {
  profileId: string; value: Settings; onChange: (value: Settings) => void;
}) {
  const { data, busy, failure, refresh } = useCodexStatus(profileId);
  const model = data?.models.find((item) => item.model === value.model);
  const proxy = proxyFields(value.proxy);
  function updateProxy(update: Partial<typeof proxy>) {
    const next = { ...proxy, ...update };
    const address = next.address.includes(":") && !next.address.startsWith("[") ? `[${next.address}]` : next.address;
    onChange({ ...value, proxy: `${next.protocol}://${next.login ? `${encodeURIComponent(next.login)}@` : ""}${address}${next.port ? `:${next.port}` : ""}` });
  }
  return <FieldGroup>
    <div role="status" className="space-y-3 rounded-xl border border-white/10 bg-white/[0.02] p-3.5 text-sm">
      <p>{data?.error || (data?.proxy_state === "verified" ? "Защищённый канал Codex подключён" : "Подключите Codex через проверку изоляции и прокси")}</p>
      <p className="text-xs text-muted-foreground">{data?.reason || "Загрузка локального состояния…"}</p>
      <p className="text-xs">Авторизация: {data?.auth_state === "authenticated" ? "вход выполнен" : data?.auth_state === "login_pending" ? "ожидается подтверждение" : data?.auth_state === "not_authenticated" ? "вход не выполнен" : "не проверена"}.</p>
      {data?.auth_state === "authenticated" && <p className="text-xs text-muted-foreground">Аккаунт сохранён. При следующем запуске kid подключится автоматически.</p>}
      {data?.exit_ip && <p className="text-xs">Выход через прокси: {data.exit_ip}</p>}
      {data?.login && <div className="space-y-2 rounded-xl border border-white/10 bg-white/[0.04] p-3">
        <p>Код входа: <strong className="select-all font-mono">{data.login.user_code}</strong></p>
        <p className="select-all break-all font-mono text-xs">{data.login.verification_url}</p>
        <p className="text-xs text-muted-foreground">Подтвердите код на другом устройстве или в браузере с защищённым прокси. kid не открывает браузер компьютера автоматически.</p>
      </div>}
      {failure && <p className="text-xs text-destructive" role="alert">{failure}</p>}
      <div className="flex flex-wrap gap-2">
        {data?.auth_state !== "authenticated" && <Button type="button" size="sm" disabled={!data?.capabilities.login || busy} onClick={() => void refresh("login")}>Войти в ChatGPT</Button>}
        {data?.proxy_state !== "verified" && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void refresh("retry")}>Подключить через прокси</Button>}
        {data?.auth_state === "authenticated" && <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void refresh("logout")}>Выйти</Button>}
      </div>
    </div>
    <Field>
      <div className="flex items-center justify-between gap-3">
        <FieldLabel htmlFor="codex-auto-commands">Автоматически разрешать команды</FieldLabel>
        <Switch id="codex-auto-commands" checked={value.auto_approve_commands ?? false}
          onCheckedChange={(checked) => onChange({ ...value, auto_approve_commands:checked })} />
      </div>
      <FieldDescription>Команды PowerShell и CMD выполняются на вашем компьютере. При включении они запускаются без подтверждения и могут менять файлы. Прокси защищает соединения самого Codex; команды используют обычную сеть Windows.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="codex-proxy-protocol">Тип прокси</FieldLabel>
      <Select value={proxy.protocol} onValueChange={(protocol) => { if (protocol) updateProxy({ protocol }); }}>
        <SelectTrigger id="codex-proxy-protocol" className="w-full"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="http">HTTP</SelectItem>
          <SelectItem value="https">HTTPS</SelectItem>
          <SelectItem value="socks5h">SOCKS5</SelectItem>
        </SelectContent>
      </Select>
    </Field>
    <div className="grid grid-cols-[minmax(0,1fr)_6rem] gap-3">
      <Field className="min-w-0">
        <FieldLabel htmlFor="codex-proxy-address">Адрес прокси</FieldLabel>
        <Input id="codex-proxy-address" value={proxy.address} autoComplete="off" spellCheck={false} placeholder="127.0.0.1" onChange={(event) => updateProxy({ address: event.target.value.trim() })} />
      </Field>
      <Field>
        <FieldLabel htmlFor="codex-proxy-port">Порт</FieldLabel>
        <Input id="codex-proxy-port" value={proxy.port} inputMode="numeric" autoComplete="off" placeholder="8080" onChange={(event) => updateProxy({ port: event.target.value.replace(/\D/g, "") })} />
      </Field>
    </div>
    <Field>
      <FieldLabel htmlFor="codex-proxy-login">Логин прокси</FieldLabel>
      <Input id="codex-proxy-login" value={proxy.login} autoComplete="off" spellCheck={false} placeholder="Логин" onChange={(event) => updateProxy({ login: event.target.value })} />
    </Field>
    <Field>
      <FieldLabel htmlFor="codex-proxy-password">Пароль прокси</FieldLabel>
      <SecretInput key={profileId} id="codex-proxy-password" label="Пароль прокси" cleared={value.clear_proxy_password}
        value={value.proxy_password ?? ""} saved={value.has_proxy_password && !value.clear_proxy_password}
        reveal={{field:"codex_proxy_password", profile_id:profileId}}
        onChange={(password) => onChange({ ...value, proxy_password:password, clear_proxy_password:false })} />
      <Button type="button" variant="ghost" size="sm" onClick={() => onChange({ ...value, proxy_password:"", clear_proxy_password:true })}>Удалить пароль при сохранении</Button>
      <FieldDescription>Укажите числовой IP и порт прокси. Сохраните профиль перед подключением. Прямой маршрут запрещён.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="codex-model">Модель Codex</FieldLabel>
      <Select value={value.model} disabled={!data?.models.length} onValueChange={(model) => { if (model !== null) onChange({ ...value, model, effort: "" }); }}>
        <SelectTrigger id="codex-model" className="w-full"><SelectValue>{model?.displayName || value.model || (data?.models.length ? "По умолчанию App Server" : "Список моделей недоступен")}</SelectValue></SelectTrigger>
        <SelectContent>
          <SelectItem value="">По умолчанию App Server</SelectItem>
          {value.model && !model && <SelectItem value={value.model}>{value.model}</SelectItem>}
          {data?.models.map((item) => <SelectItem key={item.id} value={item.model}>{item.displayName}</SelectItem>)}
        </SelectContent>
      </Select>
      <FieldDescription>Список поступает из model/list. Доступ к конкретной модели подтверждается успешным запросом.</FieldDescription>
    </Field>
    <Field>
      <FieldLabel htmlFor="codex-effort">Уровень рассуждений</FieldLabel>
      <Select value={value.effort} disabled={!model} onValueChange={(effort) => { if (effort !== null) onChange({ ...value, effort }); }}>
        <SelectTrigger id="codex-effort" className="w-full"><SelectValue>{value.effort || "По умолчанию модели"}</SelectValue></SelectTrigger>
        <SelectContent>
          <SelectItem value="">По умолчанию модели</SelectItem>
          {value.effort && !model?.supportedReasoningEfforts.some((item) => item.reasoningEffort === value.effort) && <SelectItem value={value.effort}>{value.effort}</SelectItem>}
          {model?.supportedReasoningEfforts.map((item) => <SelectItem key={item.reasoningEffort} value={item.reasoningEffort}>{item.reasoningEffort}</SelectItem>)}
        </SelectContent>
      </Select>
    </Field>
    <Field>
      <FieldLabel htmlFor="codex-search">Поисковый движок</FieldLabel>
      <Select value={value.search} onValueChange={(search) => { if (search) onChange({ ...value, search: search as Settings["search"] }); }}>
        <SelectTrigger id="codex-search" className="w-full"><SelectValue>{value.search === "kid" ? "Поиск kid" : "Поиск Codex"}</SelectValue></SelectTrigger>
        <SelectContent><SelectItem value="codex">Поиск Codex</SelectItem><SelectItem value="kid">Поиск kid</SelectItem></SelectContent>
      </Select>
      <FieldDescription>Поиск Codex использует встроенный поиск. Поиск kid отключает встроенный поиск и предоставляет инструменты поиска и чтения kid через локальный MCP. Сетевые команды Windows работают отдельно от этих инструментов.</FieldDescription>
    </Field>
  </FieldGroup>;
}
