/**
 * Контракты Document Studio.
 *
 * Никаких встроенных пресетов, демо-документов и «заготовленных» правил здесь
 * нет: пресеты и результаты анализа/компиляции приходят из
 * `services/word_automation` через мост приложения, а содержимое документа —
 * из ответов бэкенда (модель предпросмотра).
 */

/** Базовый адрес моста — тот же, что используют остальные части интерфейса. */
export const STUDIO_BRIDGE_BASE = "";

/** Ключ рабочего документа студии на бэкенде (общий для всех её чатов). */
export const STUDIO_SESSION_ID = "document-studio";

/** Ключ в localStorage: активный чат студии. */
export const STUDIO_LAST_SESSION_KEY = "kid-studio-last-session";

/* ------------------------------------------------------------------ пресет оформления
 * Форма совпадает с `StylePreset.model_dump()` из
 * services/word_automation/preset_schema.py — ровно то, что отдаёт
 * GET /api/word/presets и POST /api/word/analyze.
 */

export interface StudioPageSettings {
  top: number;
  bottom: number;
  left: number;
  right: number;
  orientation: "portrait" | "landscape";
}

export interface StudioBodySettings {
  font_name: string;
  font_size: number;
  color: string;
  alignment: string;
  line_spacing: number;
  line_spacing_rule: string;
  first_line_indent: number;
  space_before: number;
  space_after: number;
  keep_with_next: boolean;
  widow_control: boolean;
}

export interface StudioHeadingSettings {
  font_name: string;
  font_size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  text_case: string;
  alignment: string;
  space_before: number;
  space_after: number;
  page_break_before: boolean;
  keep_with_next: boolean;
  first_line_indent: number;
}

export interface StudioTableSettings {
  font_name: string;
  font_size: number;
  color: string;
  alignment: string;
  header_bold: boolean;
  header_alignment: string | null;
  borders: boolean;
  border_size: number;
  border_color: string;
  repeat_header: boolean;
  cant_split: boolean;
  space_before: number;
  space_after: number;
  table_alignment: string | null;
  cell_margin_top: number;
  cell_margin_bottom: number;
  cell_margin_left: number;
  cell_margin_right: number;
}

export interface StudioCaptionSettings {
  figure_placement: string;
  table_placement: string;
  alignment: string;
  figure_text_format: string;
  table_text_format: string;
  font_name: string;
  font_size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  space_before: number;
  space_after: number;
}

export interface StudioFigureSettings {
  alignment: string;
}

export interface StudioTitlePageBlock {
  font_name: string;
  font_size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  alignment: string;
  space_before: number;
  space_after: number;
}

export interface StudioTitlePageSignatures extends StudioTitlePageBlock {
  left_indent: number;
  line_style: string;
  line_width: number;
  preserve_tabs: boolean;
  tab_stop_mm: number;
  tab_leader: string;
}

export interface StudioTitlePageFooter extends StudioTitlePageBlock {
  anchor_bottom: boolean;
}

export interface StudioTitlePageSettings {
  enabled: boolean;
  own_section: boolean;
  distribute_vertically: boolean;
  header: StudioTitlePageBlock;
  title: StudioTitlePageBlock;
  signatures: StudioTitlePageSignatures;
  footer: StudioTitlePageFooter;
}

export interface StudioSpecialSectionsSettings {
  enabled: boolean;
  apply_to: string[];
  title_font_name: string;
  title_font_size: number;
  title_bold: boolean;
  title_italic: boolean;
  title_color: string;
  title_alignment: string;
  title_text_case: string;
  title_first_line_indent: number;
  title_space_before: number;
  title_space_after: number;
  body_font_name: string;
  body_font_size: number;
  body_alignment: string;
  body_first_line_indent: number;
  body_line_spacing: number;
  body_line_spacing_rule: string;
  page_break_before: boolean;
  structural_titles: string[];
  toc_right_tab_mm: number;
  toc_leader: string;
  toc_rebuild_entries: boolean;
  toc_entry_alignment: string;
}

export interface StudioPageNumberingSettings {
  enabled: boolean;
  position: string;
  alignment: string;
  start_at: number;
  show_from_page: number;
  show_on_first_page: boolean;
  show_on_task_page: boolean;
  hide_on_special: boolean;
  page_format: string;
  font_name: string;
  font_size: number;
}

export interface StudioListSettings {
  bullet_marker: string;
  numbered_format: string;
  alignment: string;
  left_indent: number;
  level_indent: number;
  space_before: number;
  space_after: number;
  keep_with_next: boolean;
}

export interface StudioPreset {
  id?: string;
  name: string;
  description: string;
  page: StudioPageSettings;
  body: StudioBodySettings;
  headings: Record<string, StudioHeadingSettings>;
  tables: StudioTableSettings;
  captions: StudioCaptionSettings;
  figures: StudioFigureSettings;
  lists: StudioListSettings;
  cleanup?: Record<string, unknown>;
  title_page?: StudioTitlePageSettings;
  special_sections?: StudioSpecialSectionsSettings;
  page_numbering?: StudioPageNumberingSettings;
}

export interface StudioPreviewBlock {
  type: string;
  text?: string;
  level?: number;
  alignment?: string | null;
  fontName?: string | null;
  fontSizePt?: number | null;
  bold?: boolean | null;
  italic?: boolean | null;
  lineSpacing?: number | null;
  lineSpacingRule?: string | null;
  firstLineIndentMm?: number | null;
  spaceBeforePt?: number | null;
  spaceAfterPt?: number | null;
  rows?: string[][];
  tableAlignment?: string | null;
  cellMarginTopMm?: number | null;
  cellMarginBottomMm?: number | null;
  cellMarginLeftMm?: number | null;
  cellMarginRightMm?: number | null;
  dataUrl?: string;
  widthMm?: number | null;
  heightMm?: number | null;
}

export interface StudioPreview {
  page: StudioPageSettings;
  blocks: StudioPreviewBlock[];
}

/* ------------------------------------------------------------------ правила */

export interface StudioRule {
  scope: string;
  property: string;
  value: string | number | boolean;
}

/** Области и свойства правил — те же, что поддерживает бэкенд (RULES_CATALOG). */
export const STUDIO_RULE_SCOPES = [
  "page", "body", "headings", "h1", "h2", "h3",
  "captions", "lists", "tables", "figures",
  "title_page", "special_sections", "page_numbering",
] as const;

const _TEXT_PROPS = [
  "alignment", "space_before", "space_after", "first_line_indent",
  "font_name", "font_size", "bold", "italic",
];

export const STUDIO_RULE_PROPERTIES: Record<string, string[]> = {
  page: ["top", "bottom", "left", "right"],
  body: [..._TEXT_PROPS, "line_spacing"],
  headings: [..._TEXT_PROPS],
  h1: [..._TEXT_PROPS],
  h2: [..._TEXT_PROPS],
  h3: [..._TEXT_PROPS],
  captions: [..._TEXT_PROPS],
  lists: ["alignment", "space_before", "space_after", "left_indent"],
  tables: [
    "table_alignment", "alignment", "font_name", "font_size", "bold", "italic",
    "cell_margin_top", "cell_margin_bottom", "cell_margin_left", "cell_margin_right",
  ],
  figures: ["alignment"],
  title_page: [
    "alignment", "space_before", "space_after", "first_line_indent",
    "left_indent", "font_name", "font_size", "bold", "italic", "color",
  ],
  special_sections: [
    "alignment", "space_before", "space_after", "first_line_indent",
    "line_spacing", "font_name", "font_size", "bold", "italic", "color",
  ],
  page_numbering: [
    "enabled", "position", "alignment", "start_at", "show_from_page",
    "show_on_first_page", "show_on_task_page", "hide_on_special",
    "page_format", "font_name", "font_size",
  ],
};

export const STUDIO_ALIGNMENTS = ["left", "center", "right", "justify"] as const;

/* ------------------------------------------------------------------ обращения к мосту */

interface StudioAnalyzeResponse {
  preset?: unknown;
  preview?: unknown;
  report?: unknown;
  source_name?: string;
  preset_path?: string;
  error?: string;
}

interface StudioPreviewResponse {
  preview?: unknown;
  source_name?: string;
  error?: string;
}

interface StudioApplyResponse {
  preview?: unknown;
  path?: string;
  filename?: string;
  download?: string;
  stats?: Record<string, unknown>;
  error?: string;
}

interface StudioStateResponse {
  path?: string;
  name?: string;
  preset?: string;
  preset_json?: unknown;
  attached_presets?: unknown;
  has_document?: boolean;
  preview?: unknown;
  download?: string;
  error?: string;
}

interface StudioPresetSaveResponse {
  preset?: unknown;
  preset_path?: string;
  preset_saved?: boolean;
  name?: string;
  error?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizePreset(raw: unknown): StudioPreset | null {
  if (!isRecord(raw)) return null;
  const record = raw as Record<string, unknown>;
  return {
    ...(raw as unknown as StudioPreset),
    id: String(record.id ?? record.name ?? ""),
    name: String(record.name ?? record.id ?? ""),
    description: String(record.description ?? ""),
  };
}


/** Пресеты из word_automation. Пустой список — честный результат, фолбэков нет. */
export async function fetchStudioPresets(signal?: AbortSignal): Promise<StudioPreset[]> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/presets`, { signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = (await response.json()) as { presets?: unknown };
  if (!Array.isArray(data.presets)) return [];
  return data.presets
    .map(normalizePreset)
    .filter((item): item is StudioPreset => item !== null);
}

/** Загружает исходный DOCX на бэкенд и возвращает его `file_id`. */
export async function uploadStudioSource(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file, file.name);
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/upload`, {
    method: "POST",
    body: form,
  });
  const data = (await response.json().catch(() => ({}))) as {
    file_id?: string;
    error?: string;
  };
  if (!response.ok || !data.file_id) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data.file_id;
}

export interface StudioAnalyzeResult {
  preset: StudioPreset;
  preview: StudioPreview;
  report: Record<string, unknown>;
  sourceName: string;
  /** true, только если пресет реально записан на диск (по явному запросу). */
  presetSaved: boolean;
}

/**
 * Анализирует прикреплённый DOCX: извлекает параметры оформления и
 * регистрирует файл как рабочий документ студии.
 *
 * Пресет при этом НЕ создаётся автоматически — раньше каждый анализ писал новый
 * файл в каталог пресетов, и список разрастался дубликатами. Сохранение теперь
 * явное: `extractStudioPreset()` (кнопка «Извлечь пресет») либо `presetName`.
 */
export async function analyzeStudioDocument(
  fileId: string,
  presetName?: string,
): Promise<StudioAnalyzeResult> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: STUDIO_SESSION_ID,
      file_id: fileId,
      preset_name: presetName ?? "",
    }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioAnalyzeResponse;
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  const preset = normalizePreset(data.preset);
  if (!preset) throw new Error("Бэкенд не вернул пресет");
  return {
    preset,
    preview: (data.preview as StudioPreview) ?? { page: preset.page, blocks: [] },
    report: isRecord(data.report) ? data.report : {},
    sourceName: String(data.source_name ?? ""),
    presetSaved: Boolean(data.preset_path),
  };
}

/** Возвращает свежую модель предпросмотра текущего документа студии. */
export async function previewStudioDocument(): Promise<StudioPreview | null> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: STUDIO_SESSION_ID }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioPreviewResponse;
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return (data.preview as StudioPreview) ?? null;
}

export interface StudioApplyResult {
  preview: StudioPreview;
  path?: string;
  download?: string;
  filename?: string;
  stats: Record<string, unknown>;
}

async function postApply(payload: Record<string, unknown>): Promise<StudioApplyResult> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: STUDIO_SESSION_ID, ...payload }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioApplyResponse;
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  // Ссылку скачивания бэкенд отдаёт относительной (/api/download-local) —
  // приклеиваем адрес моста, иначе браузер уйдёт на порт фронтенда (:3000).
  const download = data.download
    ? data.download.startsWith("http")
      ? data.download
      : `${STUDIO_BRIDGE_BASE}${data.download}`
    : undefined;
  return {
    preview: (data.preview as StudioPreview) ?? { page: {} as StudioPageSettings, blocks: [] },
    path: data.path,
    download,
    filename: data.filename,
    stats: isRecord(data.stats) ? data.stats : {},
  };
}

/** Применяет (возможно отредактированный) пресет к документу студии. */
export async function applyStudioPreset(preset: StudioPreset): Promise<StudioApplyResult> {
  return postApply({ preset });
}

/** Применяет произвольный список правил к документу студии. */
export async function applyStudioRules(rules: StudioRule[]): Promise<StudioApplyResult> {
  return postApply({ rules });
}

/** Сохраняет текущий (отредактированный) пресет на бэкенде под новым именем. */
export async function saveStudioPreset(
  preset: StudioPreset,
  name: string,
): Promise<StudioPreset> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/presets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preset, name }),
  });
  const data = (await response.json().catch(() => ({}))) as {
    preset?: unknown;
    error?: string;
  };
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  const saved = normalizePreset(data.preset);
  if (!saved) throw new Error("Не удалось сохранить пресет");
  return saved;
}

/**
 * Явно извлекает пресет из документа студии и сохраняет его на диск.
 *
 * Единственное место, где пресет создаётся: автоматическое сохранение при
 * анализе отключено, поэтому каталог пресетов больше не разрастается сам.
 */
export async function extractStudioPreset(
  preset?: StudioPreset | null,
  name?: string,
): Promise<StudioPreset> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/preset-save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: STUDIO_SESSION_ID,
      preset: preset ?? undefined,
      preset_name: name ?? "",
    }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioPresetSaveResponse;
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  const saved = normalizePreset(data.preset);
  if (!saved) throw new Error("Бэкенд не вернул извлечённый пресет");
  return saved;
}

export interface StudioDocumentState {
  hasDocument: boolean;
  path: string;
  name: string;
  presetName: string;
  preset: StudioPreset | null;
  attachedPresets: StudioPreset[];
  preview: StudioPreview | null;
  download?: string;
}

/**
 * Возвращает состояние студии с бэкенда: открытый файл, пресет, предпросмотр.
 *
 * Вызывается при возврате в Document Studio после перехода между разделами:
 * документ живёт на бэкенде, поэтому открытое состояние восстанавливается без
 * повторной загрузки файла пользователем.
 */
export async function fetchStudioState(): Promise<StudioDocumentState> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/state`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: STUDIO_SESSION_ID }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioStateResponse;
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  const attachedPresets = Array.isArray(data.attached_presets)
    ? data.attached_presets
        .map(normalizePreset)
        .filter((item): item is StudioPreset => item !== null)
    : [];
  // Ссылку скачивания бэкенд отдаёт относительной — приклеиваем адрес моста.
  const download = data.download
    ? data.download.startsWith("http")
      ? data.download
      : `${STUDIO_BRIDGE_BASE}${data.download}`
    : undefined;
  return {
    hasDocument: Boolean(data.has_document),
    path: String(data.path ?? ""),
    name: String(data.name ?? ""),
    presetName: String(data.preset ?? ""),
    preset: normalizePreset(data.preset_json),
    attachedPresets,
    preview: (data.preview as StudioPreview) ?? null,
    download,
  };
}

/**
 * Закрывает документ студии: сбрасывает файл, пресет и анализ на бэкенде.
 *
 * Явное действие — без него состояние сессии жило бы до перезапуска моста, и
 * следующий документ «наследовал» бы прошлый пресет.
 */
export async function closeStudioDocument(): Promise<boolean> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/word/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: STUDIO_SESSION_ID }),
  });
  const data = (await response.json().catch(() => ({}))) as {
    ok?: boolean;
    error?: string;
  };
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return Boolean(data.ok);
}


/* ------------------------------------------------------------------ профили и чаты студии
 * Document Studio полностью отделена от основных чатов: собственные профили
 * (свои модель/ключ/промпт) и собственный список чатов на бэкенде.
 */

export interface StudioProfile {
  id: string;
  name: string;
  emoji: string;
  description: string;
  modelName: string;
  systemPrompt: string;
  temperature: number;
  baseUrl: string;
  apiKey: string;
  hasApiKey?: boolean;
  clearApiKey?: boolean;
  requestTimeout: number;
}

export interface StudioSession {
  id: string;
  title: string;
  personaId: string;
  timestamp: string;
}

export interface StudioInit {
  profiles: StudioProfile[];
  active_profile: string;
  sessions: StudioSession[];
  last_active_session: string;
}

export interface StudioHistoryMessage {
  role: string;
  content?: unknown;
  display?: string;
}

/** Профили + список чатов студии (GET /api/studio/init). */
export async function fetchStudioInit(signal?: AbortSignal): Promise<StudioInit> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/studio/init`, { signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = (await response.json().catch(() => ({}))) as Partial<StudioInit>;
  return {
    profiles: Array.isArray(data.profiles) ? (data.profiles as StudioProfile[]) : [],
    active_profile: String(data.active_profile ?? ""),
    sessions: Array.isArray(data.sessions) ? (data.sessions as StudioSession[]) : [],
    last_active_session: String(data.last_active_session ?? ""),
  };
}

async function studioPost(
  path: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) throw new Error(String(data.error || `HTTP ${response.status}`));
  return data;
}

export const saveStudioProfile = (profile: StudioProfile) =>
  studioPost("/api/studio/save_profile", { persona: profile });

export const deleteStudioProfile = (profileId: string) =>
  studioPost("/api/studio/delete_profile", { profile_id: profileId });

export const setStudioActiveProfile = (profileId: string) =>
  studioPost("/api/studio/set_active_profile", { profile_id: profileId });

export const upsertStudioSession = (session: {
  id: string;
  title?: string;
  personaId?: string;
}) => studioPost("/api/studio/session/upsert", session);

export const deleteStudioSession = (sessionId: string) =>
  studioPost("/api/studio/session/delete", { session_id: sessionId });

export const clearStudioSession = (sessionId: string) =>
  studioPost("/api/studio/session/clear", { session_id: sessionId });

/** История одного чата студии (для восстановления ленты при переключении). */
export async function fetchStudioMessages(sessionId: string): Promise<StudioHistoryMessage[]> {
  const data = await studioPost("/api/studio/session/messages", { session_id: sessionId });
  return Array.isArray(data.messages)
    ? (data.messages as StudioHistoryMessage[])
    : [];
}

/**
 * Скачивает готовый документ через blob.
 *
 * Прямая навигация по ссылке `<a href="/api/download-local...">` в WebView
 * обрывается (ERR_EMPTY_RESPONSE). Скачиваем содержимое запросом и отдаём его
 * браузеру через объектную ссылку — работает и в WebView2, и в обычном браузере.
 */
export async function downloadStudioResult(
  url: string,
  filename = "document.docx",
): Promise<void> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename || "document.docx";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 2000);
}

export interface StudioSaveResult {
  ok: boolean;
  canceled?: boolean;
  path?: string;
}

/**
 * Нативное «Сохранить как» через бэкенд.
 *
 * В WebView2 скриптовое скачивание blob-ссылки может молча игнорироваться,
 * поэтому файл на диск пишет сам Python-мост (диалог сохранения + копирование).
 */
export async function saveStudioResult(
  path: string,
  name: string,
): Promise<StudioSaveResult> {
  const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/save-file`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, name }),
  });
  const data = (await response.json().catch(() => ({}))) as StudioSaveResult & {
    error?: string;
  };
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
