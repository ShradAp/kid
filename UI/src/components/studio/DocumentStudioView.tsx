"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { DocumentA4Canvas } from "@/components/studio/DocumentA4Canvas";
import { StudioChatConsole } from "@/components/studio/StudioChatConsole";
import { StudioInspectorPanel } from "@/components/studio/StudioInspectorPanel";
import type { Message } from "@/lib/assistant-data";
import {
  STUDIO_BRIDGE_BASE,
  analyzeStudioDocument,
  applyStudioPreset,
  applyStudioRules,
  clearStudioSession,
  closeStudioDocument,
  downloadStudioResult,
  extractStudioPreset,
  fetchStudioMessages,
  fetchStudioPresets,
  fetchStudioState,
  previewStudioDocument,
  saveStudioPreset,
  saveStudioResult,
  uploadStudioSource,
  type StudioDocumentState,
  type StudioHistoryMessage,
  type StudioPreview,
  type StudioPreset,
  type StudioRule,
} from "@/lib/studio-data";

function timestampNow(): string {
  return new Date().toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isDocx(file: File): boolean {
  return /\.doc[xm]$/i.test(file.name);
}

/** Восстанавливает ленту сообщений из истории чата студии. */
function historyToMessages(history: StudioHistoryMessage[]): Message[] {
  const out: Message[] = [];
  history.forEach((item, index) => {
    if (!item || (item.role !== "user" && item.role !== "assistant")) return;
    const content =
      item.display && item.display.trim()
        ? item.display
        : typeof item.content === "string"
          ? item.content
          : "";
    if (!content.trim()) return;
    out.push({
      id: `studio_hist_${index}_${item.role}`,
      role: item.role,
      content,
      timestamp: "",
    });
  });
  return out;
}

interface StudioChatReply {
  reply?: string;
  error?: string;
}

/**
 * Document Studio: чат-консоль ассистента, предпросмотр рабочего DOCX и
 * инспектор-редактор оформления. Файл, прикреплённый к студии, живёт на
 * бэкенде: модель правит его инструментами, а не получает как вложение чата.
 * Профили и чаты студии ведутся снаружи (боковая панель приложения) и
 * передаются сюда как `sessionId`.
 */
export function DocumentStudioView({
  fontSizeIndex = 1,
  sessionId,
}: {
  fontSizeIndex?: number;
  sessionId: string;
}) {
  const [presets, setPresets] = useState<StudioPreset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [draft, setDraft] = useState<StudioPreset | null>(null);
  const [preview, setPreview] = useState<StudioPreview | null>(null);
  const [rules, setRules] = useState<StudioRule[]>([]);
  const [attachedPresets, setAttachedPresets] = useState<StudioPreset[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [liveSteps, setLiveSteps] = useState<string[]>([]);
  const [sourceName, setSourceName] = useState<string | null>(null);
  const [sourceFileId, setSourceFileId] = useState<string | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isApplying, setIsApplying] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isExtracting, setIsExtracting] = useState(false);
  const [isClosing, setIsClosing] = useState(false);
  const [result, setResult] = useState<{
    download?: string;
    filename?: string;
    path?: string;
  }>({});
  const [isDragging, setIsDragging] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Пресеты оформления И состояние открытого документа — из services/word_automation
  // через мост приложения.
  //
  // Состояние тянем ПЕРВЫМ: документ живёт на бэкенде, поэтому при возврате в
  // Document Studio после перехода между разделами открытый файл, его пресет и
  // предпросмотр восстанавливаются сами — без повторной загрузки пользователем.
  // Если документ открыт, его пресет важнее «первого из списка», иначе панель
  // показывала бы чужой пресет поверх рабочего документа.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      let restored: StudioDocumentState | null = null;
      try {
        restored = await fetchStudioState();
      } catch {
        // Мост мог быть не готов — не критично, остаёмся на пустом состоянии.
        restored = null;
      }
      if (controller.signal.aborted) return;
      if (restored?.hasDocument) {
        setSourceName(restored.name || null);
        // Бэкенд принимает абсолютный путь там же, где file_id, поэтому путь
        // годится для повторного анализа после восстановления.
        setSourceFileId(restored.path || null);
        setPreview(restored.preview);
        setAttachedPresets(restored.attachedPresets);
        if (restored.download) {
          setResult({ download: restored.download, filename: restored.name });
        }
      }
      try {
        const loaded = await fetchStudioPresets(controller.signal);
        if (controller.signal.aborted) return;
        setPresets(loaded);
        if (restored?.preset) {
          setPresetId(restored.preset.id ?? restored.preset.name);
          setDraft(restored.preset);
        } else if (loaded[0]) {
          setPresetId(loaded[0].id ?? loaded[0].name);
          setDraft(loaded[0]);
        }
      } catch (error: unknown) {
        if (controller.signal.aborted) return;
        toast.error("Пресеты недоступны", {
          description: error instanceof Error ? error.message : undefined,
        });
      }
    })();
    return () => controller.abort();
  }, []);

  // Чат студии: лента грузится из её собственной истории при смене активного чата
  // (переключение делается снаружи — боковой панелью приложения).
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    fetchStudioMessages(sessionId)
      .then((history) => {
        if (!cancelled) setMessages(historyToMessages(history));
      })
      .catch(() => {
        if (!cancelled) setMessages([]);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);


  function selectPreset(nextId: string) {
    setPresetId(nextId);
    const found = presets.find((item) => (item.id ?? item.name) === nextId);
    if (found) setDraft(found);
    setResult({});
  }

  async function requestReply(
    prompt: string,
    presets?: StudioPreset[] | null,
  ): Promise<string> {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const response = await fetch(`${STUDIO_BRIDGE_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        // Чат студии — свой id (её собственные истории), не основная сессия.
        session_id: sessionId || "document-studio",
        studio: true,
        // К сообщению можно приложить несколько пресетов — модель получает их
        // все и анализирует оформление (и может сравнивать пресеты между собой).
        presets: presets && presets.length > 0 ? presets : undefined,
      }),
      signal: controller.signal,
    });
    const data = (await response.json().catch(() => ({}))) as StudioChatReply;
    if (!response.ok || typeof data.reply !== "string" || !data.reply.trim()) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data.reply.trim();
  }

  async function refreshPreview() {
    try {
      const fresh = await previewStudioDocument();
      if (fresh) setPreview(fresh);
    } catch {
      // Предпросмотр не критичен: ошибку не показываем, оставляем прежний.
    }
  }

  async function handleSend() {
    const prompt = input.trim();
    if (!prompt || isGenerating) return;
    setInput("");
    setMessages((prev) => [
      ...prev,
      { id: `studio_user_${Date.now()}`, role: "user", content: prompt, timestamp: timestampNow() },
    ]);
    setIsGenerating(true);
    setLiveSteps([]);
    try {
      const reply = await requestReply(
        prompt,
        attachedPresets.length > 0 ? attachedPresets : undefined,
      );
      setMessages((prev) => [
        ...prev,
        {
          id: `studio_assistant_${Date.now()}`,
          role: "assistant",
          content: reply,
          timestamp: timestampNow(),
        },
      ]);
      // Ответ модели НЕ пишется в документ: только чат и явные инструменты/кнопки
      // меняют файл. Предпросмотр обновляем лишь потому, что инструменты могли
      // физически поправить документ.
      setAttachedPresets([]);
      if (sourceName) await refreshPreview();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      toast.error("Ассистент недоступен", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsGenerating(false);
      setLiveSteps([]);
    }
  }

  function handleStop() {
    abortRef.current?.abort();
    setIsGenerating(false);
  }

  function handleClearChat() {
    setMessages([]);
    setResult({});
    // Чистим и историю этого чата студии на бэкенде.
    if (sessionId) {
      void clearStudioSession(sessionId).catch(() => undefined);
    }
  }

  /** Прикладывает текущий пресет к следующему сообщению чата (можно несколько). */
  function handleAddPresetToChat() {
    if (!draft) return;
    const id = draft.id ?? draft.name;
    setAttachedPresets((prev) => {
      if (prev.some((item) => (item.id ?? item.name) === id)) {
        toast.info("Этот пресет уже приложен к чату");
        return prev;
      }
      toast.success("Пресет добавлен в чат", {
        description: "Добавьте ещё пресеты или отправьте сообщение",
      });
      return [...prev, draft];
    });
  }

  function handleRemoveAttachedPreset(id: string) {
    setAttachedPresets((prev) => prev.filter((item) => (item.id ?? item.name) !== id));
  }

  async function analyzeWithFileId(fileId: string, fallbackName?: string) {
    const res = await analyzeStudioDocument(fileId);
    // Пресет из анализа НЕ добавляем в список сохранённых: он живёт только в
    // сессии, пока пользователь явно не извлечёт его («Извлечь пресет»). Иначе
    // список пресетов разрастался «фантомами» после каждого анализа файла.
    setDraft(res.preset);
    setPresetId(res.presetSaved ? (res.preset.id ?? res.preset.name) : "");
    setPreview(res.preview);
    setSourceName(res.sourceName || fallbackName || null);
    setRules([]);
    setResult({});
    return res;
  }

  async function handleAttachMany(files: File[]) {
    const docx = files.find(isDocx);
    if (!docx) {
      toast.error("Ожидается файл .docx");
      return;
    }
    setIsAnalyzing(true);
    try {
      const fileId = await uploadStudioSource(docx);
      setSourceFileId(fileId);
      await analyzeWithFileId(fileId, docx.name);
      toast.success("Оформление распознано", {
        description: "Параметры наполнили панель, файл прикреплён к студии",
      });
    } catch (error) {
      toast.error("Не удалось проанализировать файл", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsAnalyzing(false);
    }
  }

  async function handleReanalyze() {
    if (!sourceFileId) {
      toast.error("Нет прикреплённого файла");
      return;
    }
    setIsAnalyzing(true);
    try {
      await analyzeWithFileId(sourceFileId, sourceName ?? undefined);
      toast.success("Оформление переанализировано");
    } catch (error) {
      toast.error("Не удалось переанализировать", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsAnalyzing(false);
    }
  }

  async function handleApply() {
    if (!draft) return;
    setIsApplying(true);
    try {
      let applied = await applyStudioPreset(draft);
      if (rules.length > 0) applied = await applyStudioRules(rules);
      setPreview(applied.preview);
      setResult({
        download: applied.download,
        filename: applied.filename,
        path: applied.path,
      });
      toast.success("Правила применены к документу", {
        description: applied.filename ?? undefined,
      });
    } catch (error) {
      toast.error("Не удалось применить правила", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsApplying(false);
    }
  }

  async function handleSavePreset(name: string) {
    if (!draft) return;
    setIsSaving(true);
    try {
      const saved = await saveStudioPreset(draft, name);
      const id = saved.id ?? saved.name;
      setPresets((prev) => [saved, ...prev.filter((p) => (p.id ?? p.name) !== id)]);
      setPresetId(id);
      setDraft(saved);
      toast.success("Пресет сохранён", { description: saved.name });
    } catch (error) {
      toast.error("Не удалось сохранить пресет", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsSaving(false);
    }
  }

  /**
   * Явно извлекает пресет из открытого документа и кладёт его на диск.
   *
   * Автоматическое создание пресета при анализе отключено, поэтому это действие
   * (вместе с «Сохранить как новый пресет») — единственный способ пополнить
   * список пресетов.
   */
  async function handleExtractPreset() {
    if (!draft) {
      toast.error("Нет параметров для извлечения", {
        description: "Сначала прикрепите и проанализируйте документ",
      });
      return;
    }
    setIsExtracting(true);
    try {
      const saved = await extractStudioPreset(draft);
      const id = saved.id ?? saved.name;
      setPresets((prev) => [saved, ...prev.filter((p) => (p.id ?? p.name) !== id)]);
      setPresetId(id);
      setDraft(saved);
      toast.success("Пресет извлечён", { description: saved.name });
    } catch (error) {
      toast.error("Не удалось извлечь пресет", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsExtracting(false);
    }
  }

  /**
   * Закрывает документ студии: сбрасывает файл, пресет и анализ на бэкенде
   * и очищает панель. Без этого состояние сессии жило бы до перезапуска моста,
   * и следующий документ «наследовал» бы прошлый пресет и предпросмотр.
   */
  async function handleCloseDocument() {
    setIsClosing(true);
    try {
      await closeStudioDocument();
      setSourceName(null);
      setSourceFileId(null);
      setPreview(null);
      setResult({});
      setRules([]);
      setAttachedPresets([]);
      toast.success("Документ закрыт");
    } catch (error) {
      toast.error("Не удалось закрыть документ", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setIsClosing(false);
    }
  }

  async function handleDownload() {
    const filename = result.filename || "document.docx";
    // 1) Нативное «Сохранить как» через бэкенд — надёжно в WebView2.
    if (result.path) {
      try {
        const saved = await saveStudioResult(result.path, filename);
        if (saved.canceled) return;
        if (saved.ok) {
          toast.success("Файл сохранён", { description: saved.path });
          return;
        }
      } catch (error) {
        // Если мост старый (нет /api/save-file) — падаем на скачивание браузером.
        const http404 = error instanceof Error && /HTTP 404/.test(error.message);
        if (!http404) {
          toast.error("Не удалось сохранить файл", {
            description: error instanceof Error ? error.message : undefined,
          });
          return;
        }
      }
    }
    if (!result.download) {
      toast.error("Файл ещё не сформирован");
      return;
    }
    try {
      await downloadStudioResult(result.download, filename);
    } catch (error) {
      toast.error("Не удалось скачать файл", {
        description: error instanceof Error ? error.message : undefined,
      });
    }
  }

  const status = isApplying
    ? "применяется"
    : result.download
      ? "применено"
      : sourceName
        ? "готов"
        : "нет файла";

  return (
    <main
      className="relative flex min-h-0 flex-1 overflow-hidden bg-background"
      onDragOver={(event) => {
        event.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return;
        setIsDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setIsDragging(false);
        const files = event.dataTransfer.files;
        if (files && files.length > 0) void handleAttachMany(Array.from(files));
      }}
    >
      {isDragging && (
        <div className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center border-2 border-dashed border-primary bg-black/70 backdrop-blur-sm">
          <div className="rounded-xl bg-card p-6 text-center shadow-2xl">
            <p className="text-base font-semibold text-primary">
              Отпустите DOCX для анализа
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Оформление распознается, файл станет рабочим документом студии
            </p>
          </div>
        </div>
      )}

      <StudioChatConsole
        messages={messages}
        isGenerating={isGenerating}
        liveSteps={liveSteps}
        input={input}
        onInputChange={setInput}
        onSend={handleSend}
        onStop={handleStop}
        onClearChat={handleClearChat}
        attachments={[]}
        onAttachMany={handleAttachMany}
        onRemoveAttachment={() => {}}
        presetChips={attachedPresets.map((item) => ({
          id: item.id ?? item.name,
          name: item.name,
        }))}
        onRemovePreset={handleRemoveAttachedPreset}
        fontSizeIndex={fontSizeIndex}
      />

      <DocumentA4Canvas preset={draft} preview={preview} />

      <StudioInspectorPanel
        presets={presets}
        presetId={presetId}
        onSelectPreset={selectPreset}
        draft={draft}
        onChangeDraft={setDraft}
        rules={rules}
        onChangeRules={setRules}
        sourceName={sourceName}
        isAnalyzing={isAnalyzing}
        onSelectSourceFile={(file) => void handleAttachMany([file])}
        onCloseDocument={() => void handleCloseDocument()}
        isClosing={isClosing}
        onExtractPreset={() => void handleExtractPreset()}
        isExtracting={isExtracting}
        onReanalyze={() => void handleReanalyze()}
        onApply={() => void handleApply()}
        isApplying={isApplying}
        onAddPresetToChat={handleAddPresetToChat}
        attachedPresetNames={attachedPresets.map((item) => item.name)}
        onSavePreset={(name) => void handleSavePreset(name)}
        isSaving={isSaving}
        onDownload={
          result.path || result.download ? () => void handleDownload() : undefined
        }
        status={status}
      />
    </main>
  );
}
