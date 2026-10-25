"use client";

import { ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Type,
  Archive,
  Search,
  Globe,
  FileText,
  Wrench,
  ChevronDown,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Field,
  FieldLabel,
  FieldGroup,
  FieldDescription,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { setFileClickMode } from "@/lib/file-actions";

const FONT_SIZE_LABELS = ["Small", "Medium", "Large", "Extra Large"];

const PROXY_MODE_ITEMS: Record<string, string> = {
  auto: "Авто — системный прокси Windows, иначе напрямую",
  direct: "Без прокси — всегда прямой доступ",
  custom: "Свой прокси — указать вручную",
};

const TOOL_SEND_MODE_ITEMS: Record<string, string> = {
  gated: "По тексту запроса (маркеры)",
  all: "Всегда все инструменты",
  embedding: "Эмбеддинг-роутер (по смыслу)",
};

function SettingsSection({
  id,
  icon,
  title,
  open,
  onToggle,
  children,
}: {
  id: string;
  icon: ReactNode;
  title: string;
  open: boolean;
  onToggle: (id: string) => void;
  children: ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-white/10 bg-white/[0.02]">
      <button
        type="button"
        onClick={() => onToggle(open ? "" : id)}
        className="flex w-full items-center justify-between gap-2.5 px-3.5 py-3 text-left transition-colors hover:bg-white/[0.04]"
      >
        <span className="flex items-center gap-2 text-sm font-medium text-foreground">
          {icon}
          {title}
        </span>
        <ChevronDown
          className={`size-4 shrink-0 text-muted-foreground transition-transform ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>
      {open && (
        <div className="border-t border-white/10 px-3.5 py-3.5">{children}</div>
      )}
    </div>
  );
}

export function SettingsModal({
  open,
  onOpenChange,
  fontSizeIndex,
  onFontSizeChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fontSizeIndex: number;
  onFontSizeChange: (index: number) => void;
}) {
  const [backupInProgress, setBackupInProgress] = useState(false);
  const [backupCount, setBackupCount] = useState<number | null>(null);
  const [backupMessage, setBackupMessage] = useState("");
  const [searchApiKey, setSearchApiKey] = useState("");
  const [hasSearchApiKey, setHasSearchApiKey] = useState(false);
  const [searchSaveMessage, setSearchSaveMessage] = useState("");
  const [researchMode, setResearchMode] = useState("adaptive");
  const [researchModeSaving, setResearchModeSaving] = useState(false);
  const [researchModeMessage, setResearchModeMessage] = useState("");
  const [proxyMode, setProxyMode] = useState("auto");
  const [proxyUrl, setProxyUrl] = useState("");
  const [proxyUser, setProxyUser] = useState("");
  const [proxyPass, setProxyPass] = useState("");
  const [hasProxyPass, setHasProxyPass] = useState(false);
  const [clearProxyPass, setClearProxyPass] = useState(false);
  const [proxySaving, setProxySaving] = useState(false);
  const [proxyMessage, setProxyMessage] = useState("");
  const [fileMode, setFileMode] = useState<"download" | "preview">("download");
  const [fileModeMessage, setFileModeMessage] = useState("");
  const [downloadBasePath, setDownloadBasePath] = useState("");
  const [downloadPathMessage, setDownloadPathMessage] = useState("");
  const [toolSendMode, setToolSendMode] = useState("gated");
  const [toolSendModeMessage, setToolSendModeMessage] = useState("");
  const [openSection, setOpenSection] = useState("backup");

  useEffect(() => {
    if (!open) return;
    fetch("/api/init")
      .then((response) => response.json())
      .then((data: { search_api_key?: string; has_search_api_key?: boolean; search_research_mode?: string }) => {
        setSearchApiKey(data.search_api_key ?? "");
        setHasSearchApiKey(Boolean(data.has_search_api_key));
        setResearchMode(data.search_research_mode === "isolated" ? "isolated" : "adaptive");
      })
      .catch(() => setSearchSaveMessage("Could not load"));
    // Настройки прокси (вкладка «Прокси»)
    fetch("/api/proxy-settings")
      .then((response) =>
        response.ok
          ? response.json()
          : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then(
        (
          data: {
            mode?: string;
            url?: string;
            username?: string;
            password?: string;
            has_password?: boolean;
          } | null,
        ) => {
          if (!data) return;
          setProxyMode(data.mode ?? "auto");
          setProxyUrl(data.url ?? "");
          setProxyUser(data.username ?? "");
          setProxyPass(data.password ?? "");
          setHasProxyPass(Boolean(data.has_password));
          setClearProxyPass(false);
        },
      )
      .catch((error) => {
        if (error instanceof Error && error.message === "HTTP 404") {
          setProxyMessage(
            "Мост отвечает старой версией кода — перезапустите приложение (закройте его и откройте WEB_UI.pyw заново)",
          );
        }
      });
    // Режим открытия созданных файлов (скачивание/предпросмотр).
    fetch("/api/file-click-mode")
      .then((response) =>
        response.ok
          ? response.json()
          : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then((data: { mode?: string }) => {
        if (data && (data.mode === "download" || data.mode === "preview")) {
          setFileMode(data.mode);
        }
      })
      .catch(() => {});
    // Базовый путь для авто-скачивания (вкладка «Файлы»).
    fetch("/api/download-base-path")
      .then((response) =>
        response.ok
          ? response.json()
          : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then((data: { path?: string }) => {
        if (data && typeof data.path === "string") {
          setDownloadBasePath(data.path);
        }
      })
      .catch(() => {});
    // Режим отправки тулов (вкладка «Tools»).
    fetch("/api/tool-send-mode")
      .then((response) =>
        response.ok
          ? response.json()
          : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then((data: { mode?: string }) => {
        if (data && typeof data.mode === "string") {
          setToolSendMode(data.mode);
        }
      })
      .catch(() => {});
  }, [open]);

  async function handleSaveSearchApiKey(value: string, clear = false) {
    setSearchApiKey(value);
    setSearchSaveMessage("Saving…");
    try {
      const response = await fetch("/api/search-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ search_api_key: value, clear_search_api_key: clear }),
      });
      if (!response.ok) throw new Error("server error");
      setSearchSaveMessage("Saved");
      setHasSearchApiKey(!clear && Boolean(value || hasSearchApiKey));
      setSearchApiKey("");
    } catch {
      setSearchSaveMessage("Could not save");
    }
  }

  async function handleResearchMode(value: string) {
    setResearchModeSaving(true);
    setResearchModeMessage("");
    try {
      const response = await fetch("/api/search-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ search_research_mode: value }),
      });
      if (!response.ok) throw new Error("server error");
      setResearchMode(value);
      setResearchModeMessage("Режим сохранён. Применится к следующему поиску.");
    } catch {
      setResearchModeMessage("Не удалось сохранить режим");
    } finally {
      setResearchModeSaving(false);
    }
  }

  async function handleSaveProxy() {
    setProxySaving(true);
    setProxyMessage("");
    try {
      const response = await fetch("/api/proxy-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: proxyMode,
          url: proxyUrl,
          username: proxyUser,
          password: proxyPass,
          clear_password: clearProxyPass,
        }),
      });
      const text = await response.text();
      let data: {
        ok?: boolean;
        verified?: boolean;
        detail?: string;
        error?: string;
      } = {};
      if (text.trim()) {
        try {
          data = JSON.parse(text) as typeof data;
        } catch {
          throw new Error(`Некорректный ответ сервера (HTTP ${response.status})`);
        }
      }
      if (!response.ok || !data.ok) {
        if (response.status === 404) {
          // Мост поднят из старого кода: python-процесс не перезагружается сам.
          throw new Error(
            "Мост отвечает старой версией кода — перезапустите приложение (закройте его полностью и откройте WEB_UI.pyw заново)",
          );
        }
        throw new Error(data.error || `HTTP ${response.status}`);
      }
      const detail = data.detail || "настройки применены";
      setProxyMessage(`${data.verified ? "✓" : "⚠"} ${detail}`);
      if (data.verified) {
        toast.success("Настройки прокси применены", { description: detail });
      } else {
        toast.warning("Прокси не прошёл проверку", { description: detail });
      }
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Не удалось сохранить настройки";
      setProxyMessage(`Ошибка: ${message}`);
      toast.error("Настройки прокси не сохранены", { description: message });
    } finally {
      setProxySaving(false);
    }
  }

  async function handleFileModeChange(mode: "download" | "preview") {
    setFileMode(mode);
    setFileModeMessage("Сохранение…");
    try {
      const response = await fetch("/api/file-click-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setFileClickMode(mode);
      setFileModeMessage("Сохранено");
    } catch {
      setFileModeMessage("Не удалось сохранить");
    }
  }

  async function handleSaveDownloadPath() {
    setDownloadPathMessage("Сохранение…");
    try {
      const response = await fetch("/api/download-base-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: downloadBasePath }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setDownloadPathMessage("Сохранено");
    } catch {
      setDownloadPathMessage("Не удалось сохранить");
    }
  }

  async function handlePickDownloadDir() {
    try {
      const response = await fetch("/api/pick-directory", { method: "POST" });
      if (!response.ok) {
        const err = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(err.error || `HTTP ${response.status}`);
      }
      const data = (await response.json().catch(() => ({}))) as { path?: string };
      if (data && typeof data.path === "string" && data.path) {
        setDownloadBasePath(data.path);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "неизвестная ошибка";
      toast.error("Не удалось открыть выбор папки", { description: message });
    }
  }

  async function handleSaveToolSendMode(mode: string) {
    setToolSendMode(mode);
    setToolSendModeMessage("Сохранение…");
    try {
      const response = await fetch("/api/tool-send-mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setToolSendModeMessage("Сохранено");
    } catch {
      setToolSendModeMessage("Не удалось сохранить");
    }
  }

  async function handleCreateBackup() {
    setBackupInProgress(true);
    setBackupMessage("");
    try {
      const response = await fetch("/api/backups", { method: "POST" });
      const responseText = await response.text();
      let data: { ok?: boolean; filename?: string; count?: number; error?: string } = {};
      if (responseText.trim()) {
        try {
          data = JSON.parse(responseText) as typeof data;
        } catch {
          throw new Error(`Сервер вернул некорректный ответ (HTTP ${response.status})`);
        }
      }
      if (!response.ok || !data.ok) {
        throw new Error(data.error || `Не удалось создать бэкап (HTTP ${response.status})`);
      }
      setBackupCount(data.count ?? null);
      setBackupMessage(`Backup created: ${data.filename ?? "архив"}`);
      toast.success("Backup created", { description: data.filename ?? "Архив создан" });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Backup could not be created";
      setBackupMessage(`Error: ${message}`);
      toast.error("Backup failed", { description: message });
    } finally {
      setBackupInProgress(false);
    }
  }

  function handleClearCache() {
    toast.success("Cache cleared", {
      description: "Local cache and session data have been wiped.",
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-white/10 bg-popover sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>
            Manage backups, appearance, and web search.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <SettingsSection
            id="backup"
            title="Резервные копии"
            icon={<Archive className="size-4 text-muted-foreground" />}
            open={openSection === "backup"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>
                  <Archive className="size-3.5 text-muted-foreground" data-icon="inline-start" />
                  Local project backups
                </FieldLabel>
                <FieldDescription>
                  Create and manage local backups of the project. Archives exclude dependencies, secrets, and temporary files.
                </FieldDescription>
                <Button onClick={handleCreateBackup} disabled={backupInProgress} className="w-full">
                  <Archive data-icon="inline-start" />
                  {backupInProgress ? "Creating backup…" : "Create Backup"}
                </Button>
                {backupMessage && <p className="text-xs text-muted-foreground">{backupMessage}</p>}
                {backupCount !== null && <p className="text-xs text-muted-foreground">Backups saved: {backupCount}/10</p>}
              </Field>
            </FieldGroup>
          </SettingsSection>

          <SettingsSection
            id="appearance"
            title="Внешний вид"
            icon={<Type className="size-4 text-muted-foreground" />}
            open={openSection === "appearance"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>
                  <Type
                    className="size-3.5 text-muted-foreground"
                    data-icon="inline-start"
                  />
                  Font size — {FONT_SIZE_LABELS[fontSizeIndex]}
                </FieldLabel>
                <Slider
                  min={0}
                  max={3}
                  step={1}
                  value={fontSizeIndex}
                  onValueChange={(v) => onFontSizeChange(v as number)}
                  className="py-2"
                />
                <div className="flex justify-between text-[11px] text-muted-foreground">
                  {FONT_SIZE_LABELS.map((label) => (
                    <span key={label}>{label}</span>
                  ))}
                </div>
              </Field>
            </FieldGroup>
          </SettingsSection>

          <SettingsSection
            id="search"
            title="Поиск"
            icon={<Search className="size-4 text-muted-foreground" />}
            open={openSection === "search"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>Режим исследования</FieldLabel>
                <Select value={researchMode} onValueChange={(value) => { if (value) void handleResearchMode(value); }} disabled={researchModeSaving}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="adaptive">Адаптивный — по умолчанию</SelectItem>
                    <SelectItem value="isolated">Отдельный оценщик</SelectItem>
                  </SelectContent>
                </Select>
                <FieldDescription>
                  {researchMode === "adaptive"
                    ? "Исследователь сохраняет историю шагов, оценивает материалы и ищет ответы на оставшиеся вопросы."
                    : "Каждую порцию материалов оценивает новый вызов той же модели: только задание, компактная память и результаты поиска."}
                </FieldDescription>
                {researchModeMessage && <p className="text-xs text-muted-foreground">{researchModeMessage}</p>}
              </Field>
              <Field>
                <FieldLabel>
                  <Search className="size-3.5 text-muted-foreground" data-icon="inline-start" />
                  Search API key
                </FieldLabel>
                <FieldDescription>
                  Serper используется для веб-поиска. Ключ общий для всех профилей; изменения применяются кнопкой сохранения.
                </FieldDescription>
                <Input
                  type="password"
                  value={searchApiKey}
                  onChange={(event) => setSearchApiKey(event.target.value)}
                  placeholder={hasSearchApiKey ? "Ключ сохранён. Введите новый для замены" : "Search API key"}
                  autoComplete="off"
                />
                {searchSaveMessage && <p className="text-xs text-muted-foreground">{searchSaveMessage}</p>}
                <div className="flex gap-2">
                  <Button type="button" size="sm" onClick={() => void handleSaveSearchApiKey(searchApiKey)}>Сохранить ключ</Button>
                  <Button type="button" size="sm" variant="ghost" onClick={() => void handleSaveSearchApiKey("", true)}>Удалить ключ</Button>
                </div>
              </Field>
            </FieldGroup>
          </SettingsSection>

          <SettingsSection
            id="proxy"
            title="Прокси"
            icon={<Globe className="size-4 text-muted-foreground" />}
            open={openSection === "proxy"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>
                  <Globe className="size-3.5 text-muted-foreground" data-icon="inline-start" />
                  Режим прокси
                </FieldLabel>
                <FieldDescription>
                  Прокси используется веб-инструментами (чтение страниц, поиск) только
                  если он реально работает — адрес проверяется тестовым запросом.
                  «Авто» подхватит прокси из настроек Windows; нерабочий адрес
                  отбрасывается в пользу прямого доступа.
                </FieldDescription>
                <Select
                  value={proxyMode}
                  onValueChange={(v) => setProxyMode(String(v))}
                  items={PROXY_MODE_ITEMS}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">
                      Авто — системный прокси Windows, иначе напрямую
                    </SelectItem>
                    <SelectItem value="direct">
                      Без прокси — всегда прямой доступ
                    </SelectItem>
                    <SelectItem value="custom">
                      Свой прокси — указать вручную
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              {proxyMode === "custom" && (
                <>
                  <Field>
                    <FieldLabel>Адрес прокси</FieldLabel>
                    <FieldDescription>
                      Формат: host:port или scheme://host:port (например, 127.0.0.1:8080)
                    </FieldDescription>
                    <Input
                      value={proxyUrl}
                      onChange={(event) => setProxyUrl(event.target.value)}
                      placeholder="127.0.0.1:8080"
                      autoComplete="off"
                    />
                  </Field>
                  <Field>
                    <FieldLabel>Логин и пароль (если требуются)</FieldLabel>
                    <div className="flex gap-2">
                      <Input
                        value={proxyUser}
                        onChange={(event) => setProxyUser(event.target.value)}
                        placeholder="логин"
                        autoComplete="off"
                      />
                      <Input
                        type="password"
                        value={proxyPass}
                        onChange={(event) => { setProxyPass(event.target.value); setClearProxyPass(false); }}
                        placeholder={hasProxyPass && !clearProxyPass ? "Пароль сохранён" : "Пароль"}
                        autoComplete="off"
                      />
                    </div>
                  </Field>
                </>
              )}

              <Button type="button" variant="ghost" size="sm" onClick={() => { setProxyPass(""); setClearProxyPass(true); }}>Удалить пароль при сохранении</Button>
              <Button onClick={handleSaveProxy} disabled={proxySaving} className="w-full">
                {proxySaving ? "Сохранение и проверка…" : "Сохранить и проверить"}
              </Button>
              {proxyMessage && (
                <p className="text-xs text-muted-foreground">{proxyMessage}</p>
              )}
            </FieldGroup>
          </SettingsSection>

          <SettingsSection
            id="files"
            title="Файлы"
            icon={<FileText className="size-4 text-muted-foreground" />}
            open={openSection === "files"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>
                  <FileText
                    className="size-3.5 text-muted-foreground"
                    data-icon="inline-start"
                  />
                  Открытие созданных файлов
                </FieldLabel>
                <FieldDescription>
                  Что происходит при нажатии на иконку скрепки в ответе модели:
                  сохранение файла через «Сохранить как» либо предпросмотр в
                  боковой панели.
                </FieldDescription>
                <div className="flex items-center justify-between gap-3 rounded-xl border border-white/10 bg-white/[0.02] p-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-foreground">
                      {fileMode === "preview" ? "Предпросмотр" : "Скачивание"}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {fileMode === "preview"
                        ? "Файл открывается в панели справа"
                        : "Файл сохраняется через диалог «Сохранить как»"}
                    </p>
                  </div>
                  <Switch
                    checked={fileMode === "preview"}
                    onCheckedChange={(checked) =>
                      void handleFileModeChange(checked ? "preview" : "download")
                    }
                    aria-label="Предпросмотр вместо скачивания"
                  />
                </div>
                {fileModeMessage && (
                  <p className="text-xs text-muted-foreground">{fileModeMessage}</p>
                )}
              </Field>
              <Field>
                <FieldLabel>Папка для скачивания</FieldLabel>
                <FieldDescription>
                  Если путь указан — файлы сохраняются туда сразу, без диалога.
                  Если оставить пустым — путь будет запрашиваться при каждом
                  скачивании.
                </FieldDescription>
                <div className="flex gap-2">
                  <Input
                    value={downloadBasePath}
                    onChange={(event) => setDownloadBasePath(event.target.value)}
                    placeholder="~/Downloads"
                    autoComplete="off"
                  />
                  <Button
                    variant="outline"
                    onClick={() => void handlePickDownloadDir()}
                    aria-label="Выбрать папку"
                  >
                    Обзор
                  </Button>
                </div>
                <div className="flex items-center gap-2">
                  <Button onClick={() => void handleSaveDownloadPath()}>
                    Сохранить путь
                  </Button>
                  {downloadPathMessage && (
                    <p className="text-xs text-muted-foreground">{downloadPathMessage}</p>
                  )}
                </div>
              </Field>
            </FieldGroup>
          </SettingsSection>

          <SettingsSection
            id="tools"
            title="Инструменты"
            icon={<Wrench className="size-4 text-muted-foreground" />}
            open={openSection === "tools"}
            onToggle={setOpenSection}
          >
            <FieldGroup>
              <Field>
                <FieldLabel>
                  <Globe className="size-3.5 text-muted-foreground" data-icon="inline-start" />
                  Отправка инструментов
                </FieldLabel>
                <FieldDescription>
                  Как решать, какие тулы показывать модели в каждом запросе: по тексту
                  запроса (маркеры), всегда все, либо по смыслу через эмбеддинг-роутер.
                </FieldDescription>
                <Select
                  value={toolSendMode}
                  onValueChange={(v) => void handleSaveToolSendMode(String(v))}
                  items={TOOL_SEND_MODE_ITEMS}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="gated">По тексту запроса (маркеры)</SelectItem>
                    <SelectItem value="all">Всегда все инструменты</SelectItem>
                    <SelectItem value="embedding">Эмбеддинг-роутер (по смыслу)</SelectItem>
                  </SelectContent>
                </Select>
                {toolSendModeMessage && (
                  <p className="text-xs text-muted-foreground">{toolSendModeMessage}</p>
                )}
              </Field>
            </FieldGroup>
          </SettingsSection>
        </div>
      </DialogContent>
    </Dialog>
  );
}
