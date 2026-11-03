"use client";

import { useRef, useState } from "react";
import { toast } from "sonner";
import type { Attachment } from "@/components/chat/input-deck";
import type { Message, AssistantStatus } from "@/lib/assistant-data";
import { uploadAttachments } from "@/components/chat/attachment-upload";
import type { useChatSessions } from "@/components/chat/use-chat-sessions";
import type { useNotes } from "@/components/panels/use-notes";

type GenerationDependencies = Pick<ReturnType<typeof useChatSessions>,
  "activePersona" | "activeSessionId" | "personas" | "setMessagesBySession" | "setSessions" | "stateMutationRef" | "persistSession" | "setBalance" | "refreshBalance"
> & Pick<ReturnType<typeof useNotes>, "handleAddNote" | "setNotes"> & {
  logsOpen: boolean;
  fetchLogs: () => void;
};

export function useChatGeneration({
  activePersona, activeSessionId, personas, setMessagesBySession, setSessions, stateMutationRef, persistSession, setBalance, refreshBalance, handleAddNote, setNotes, logsOpen, fetchLogs
}: GenerationDependencies) {
  const [liveReplyBySession, setLiveReplyBySession] = useState<Record<string, string>>({});
  // Ввод и вложения хранятся ПО СЕССИИ, чтобы текст в поле не перетекал между чатами.
  const [inputsBySession, setInputsBySession] = useState<Record<string, string>>({});
  const [attachmentsBySession, setAttachmentsBySession] = useState<
    Record<string, Attachment[]>
  >({});
  const [status, setStatus] = useState<AssistantStatus>("ready");
  // Генерация отслеживается ПО СЕССИИ: можно отправить запрос в одном чате и,
  // пока он думает, отправить другой запрос в соседнем чате.
  const [generatingBySession, setGeneratingBySession] = useState<
    Record<string, boolean>
  >({});
  // Живой прогресс генерации (GET /api/progress): вызовы инструментов
  // отображаются в реальном времени, пока /api/chat ещё не ответил.
  const [liveThinkingBySession, setLiveThinkingBySession] = useState<
    Record<string, string[]>
  >({});
  const [liveReasoningBySession, setLiveReasoningBySession] = useState<
    Record<string, string>
  >({});
  const progressTimersRef = useRef<Record<string, number>>({});
  // Активные запросы /api/chat по сессиям: контроллер отмены + сессия старта.
  // Нужны кнопке «стоп», чтобы разорвать ждущий fetch конкретного чата.
  const chatAbortsRef = useRef<
    Record<string, { controller: AbortController; sessionId: string }>
  >({});
  // Ввод, вложения, статус генерации и живой прогресс берутся по активной сессии.
  const input = inputsBySession[activeSessionId] ?? "";
  const attachments = attachmentsBySession[activeSessionId] ?? [];
  const isGenerating = Boolean(generatingBySession[activeSessionId]);
  const liveThinking = liveThinkingBySession[activeSessionId] ?? [];
  const liveReasoning = liveReasoningBySession[activeSessionId] ?? "";
  function handleAttachMany(files: File[]) {
    const newItems: Attachment[] = files.map((file) => ({
      id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      name: file.name,
      url: file.type.startsWith("image/") || file.type.startsWith("video/")
        ? URL.createObjectURL(file)
        : "",
      isImage: file.type.startsWith("image/"),
      isVideo: file.type.startsWith("video/") || /\.(mp4|webm|mov|mkv)$/i.test(file.name),
      file,
    }));
    const attachSid = activeSessionId;
    setAttachmentsBySession((prev) => ({
      ...prev,
      [attachSid]: [...(prev[attachSid] ?? []), ...newItems],
    }));
  }
  function handleRemoveAttachment(id: string) {
    const attachSid = activeSessionId;
    setAttachmentsBySession((prev) => ({
      ...prev,
      [attachSid]: (prev[attachSid] ?? []).filter((a) => a.id !== id),
    }));
  }
  function stopProgressPolling(sessionId: string) {
    setLiveReplyBySession((prev) => { const next = { ...prev }; delete next[sessionId]; return next; });
    const timer = progressTimersRef.current[sessionId];
    if (timer !== undefined) {
      window.clearInterval(timer);
      delete progressTimersRef.current[sessionId];
    }
    setLiveThinkingBySession((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
    setLiveReasoningBySession((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
  }
  function startProgressPolling(sessionId: string) {
    stopProgressPolling(sessionId);
    const poll = async () => {
      // Сессия могла быть остановлена/очищена — тик чужой генерации игнорируем.
      if (progressTimersRef.current[sessionId] === undefined) return;
      try {
        const res = await fetch(
          `/api/progress?session_id=${encodeURIComponent(sessionId)}`,
        );
        if (!res.ok) return;
        const data = (await res.json()) as {
          active?: boolean;
          steps?: unknown;
          reasoning?: unknown;
          reply?: unknown;
        };
        if (progressTimersRef.current[sessionId] === undefined) return;
        setLiveThinkingBySession((prev) => ({
          ...prev,
          [sessionId]: Array.isArray(data.steps)
            ? data.steps.map((step) => String(step))
            : [],
        }));
        setLiveReasoningBySession((prev) => ({
          ...prev,
          [sessionId]:
            typeof data.reasoning === "string" ? data.reasoning : "",
        }));
        setLiveReplyBySession((prev) => ({ ...prev, [sessionId]: typeof data.reply === "string" ? data.reply : "" }));
      } catch {
        // Мост занят обработкой /api/chat — молча ждём следующий тик.
      }
    };
    progressTimersRef.current[sessionId] = window.setInterval(poll, 1000);
    void poll();
  }
  async function handleSend() {
    // Повторная отправка во время генерации невозможна: кнопка «стоп»
    // не должна превращаться в «отправить», а Enter уже блокирован в InputDeck.
    if (isGenerating) return;
    const sessionId = activeSessionId;
    // Фиксируем ввод/вложения на момент отправки: даже если пользователь
    // переключится на другой чат во время загрузки файлов, текст уйдёт в нужную сессию.
    const sessionInput = input;
    const sessionAttachments = attachments;
    if (!sessionInput.trim() && sessionAttachments.length === 0) return;
    // Профили обязаны быть загружены: без них нет ни модели, ни ключа,
    // ни системного промпта — запрос ушёл бы «в никуда».
    if (personas.length === 0) {
      toast.error("Профили ещё не загружены — подождите пару секунд");
      return;
    }
    // The chat endpoint restores the protected connection before submitting a
    // turn. A cached status must not prevent that reconnect from ever running.
    const { accumulatedFilesPrefix, payloadAttachments, sentAttachments } =
      await uploadAttachments(sessionAttachments, activePersona);
    const filesNamesText = sessionAttachments.map((a) => a.name).join(", ");
    const displayText =
      sessionInput.trim() ||
      (sessionAttachments.length > 0 ? `Отправлены файлы: ${filesNamesText}` : "");
    const finalPromptForBackend =
      accumulatedFilesPrefix +
      (sessionInput.trim() || "Проанализируй прикрепленные файлы.");
    const userMessage: Message = {
      id: `m${Date.now()}`,
      role: "user",
      content: displayText,
      timestamp: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
      attachments: sentAttachments,
    };
    // Устаревший /api/init не должен вернуть старую историю поверх нового запроса.
    stateMutationRef.current += 1;
    setMessagesBySession((prev) => ({
      ...prev,
      [sessionId]: [...(prev[sessionId] ?? []), userMessage],
    }));
    setInputsBySession((prev) => ({ ...prev, [sessionId]: "" }));
    setAttachmentsBySession((prev) => ({ ...prev, [sessionId]: [] }));
    setGeneratingBySession((prev) => ({ ...prev, [sessionId]: true }));
    setStatus("thinking");
    startProgressPolling(sessionId);
    // Контроллер отмены: кнопка «стоп» разорвёт этот fetch и сообщит бэкенду.
    const chatController = new AbortController();
    chatAbortsRef.current[sessionId] = { controller: chatController, sessionId };
    // Финальный ответ (или ошибка) ВСЕГДА попадает в ленту активного чата:
    // ни при каком сбое сообщение не должно молча исчезнуть.
    let assistantPushed = false;
    // Баланс из тела ответа: если его там не окажется (сбой сборки ответа,
    // транспортная ошибка), в finally подтянем его отдельным запросом.
    let balanceReceived = false;
    const pushAssistant = (
      text: string,
      thinking?: string[],
      marketplaceProduct?: Message["marketplaceProduct"],
      reasoning?: string,
      activity?: Message["activity"],
      images?: Message["images"],
    ) => {
      assistantPushed = true;
      const message: Message = {
        id: `m${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        role: "assistant",
        content: text,
        timestamp: new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
        thinking: thinking && thinking.length ? thinking : undefined,
        reasoning: reasoning && reasoning.trim() ? reasoning : undefined,
        marketplaceProduct,
        activity,
        images,
      };
      setMessagesBySession((prev) => ({
        ...prev,
        [sessionId]: [...(prev[sessionId] ?? []), message],
      }));
    };
    try {
      const firstText = (displayText || finalPromptForBackend || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 48);
      const payload: any = {
        operation_id: crypto.randomUUID(),
        session_id: sessionId,
        prompt: finalPromptForBackend,
        persona_id: activePersona.id,
        max_steps: activePersona?.maxSteps ?? 60,
        attachments: payloadAttachments,
        title: firstText || "Новый чат",
      };
      // сразу показываем осмысленный заголовок в сайдбаре
      if (firstText) {
        const sid = sessionId;
        const pid = activePersona.id;
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sid && (s.title === "Новый чат" || s.title === "New chat")
              ? { ...s, title: firstText }
              : s,
          ),
        );
        persistSession(sid, pid, firstText);
      }
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: chatController.signal,
      });
      // Читаем тело текстом: даже частично битый JSON не должен терять ответ.
      const rawBody = await res.text();
      // Пользователь нажал «стоп», пока мы ждали тело — ответ не показываем.
      if (chatController.signal.aborted) return;
      let data: any = {};
      try {
        data = rawBody ? JSON.parse(rawBody) : {};
      } catch {
        data = {};
      }
      if (!res.ok) {
        throw new Error(
          typeof data?.error === "string" && data.error
            ? data.error
            : `HTTP ${res.status}`,
        );
      }
      if (typeof data.balance === "number") {
        setBalance(data.balance);
        balanceReceived = true;
      }
      const replyText =
        typeof data.reply === "string" && data.reply.trim()
          ? data.reply
          : Array.isArray(data.images) && data.images.length ? "" : "Ответ получен пустой.";
      // Ответ относится к сессии, в которой был отправлен запрос: пушим именно
      // в неё и сразу рендерим — статус загрузки снимается в finally.
      pushAssistant(
        replyText,
        Array.isArray(data.thinking) ? data.thinking : undefined,
        data.marketplaceProduct || undefined,
        typeof data.reasoning === "string" ? data.reasoning : undefined,
        data.activity,
        Array.isArray(data.images) ? data.images : undefined,
      );
      // Заметки от ассистента (tool save_note): поддерживаем пачку notes[]
      // и старый формат note, но не добавляем совместимый дубликат дважды.
      const incomingNotes: { title?: unknown; content?: unknown }[] = Array.isArray((data as any).notes)
        ? (data as any).notes
        : data.note && typeof (data as any).note.content === "string"
          ? [(data as any).note]
          : [];
      if (incomingNotes.length > 0) {
        for (const n of incomingNotes) {
          if (!n || typeof n.content !== "string" || !n.content.trim()) continue;
          handleAddNote(
            typeof n.title === "string" && n.title.trim()
              ? n.title.trim().slice(0, 120)
              : "Заметка от ассистента",
            n.content.trim().slice(0, 4000),
            "assistant",
          );
        }
        toast.success(
          incomingNotes.length > 1
            ? `Сохранено заметок: ${incomingNotes.length}`
            : "Заметка сохранена",
        );
      }
      if (logsOpen) fetchLogs();
      if (data.refresh_notes) {
        const notesResponse = await fetch("/api/notes");
        const notesData = await notesResponse.json();
        if (Array.isArray(notesData.notes)) setNotes(notesData.notes);
      }
    } catch (err) {
      // «Стоп» — штатная отмена: не пушим ошибку в ленту и не показываем тост.
      if (chatController.signal.aborted) return;
      if (activePersona?.engine === "codex" && !assistantPushed) {
        // Потеря HTTP-ответа не должна оставлять живое подтверждение/ход.
        // Отменяем, но не отправляем команду повторно: исход может быть неизвестен.
        void fetch("/api/stop", { method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({session_id:sessionId}), keepalive:true }).catch(() => undefined);
      }
      const message = err instanceof Error ? err.message : "Ошибка связи с сервером Python";
      // Гарантия: в ленте всегда появляется ответ — даже при ошибке транспорта,
      // иначе финальный ответ ассистента просто пропадал бы из интерфейса.
      if (!assistantPushed) {
        pushAssistant(`⚠️ **Не удалось получить ответ:** ${message}`);
      }
      toast.error(message);
    } finally {
      stopProgressPolling(sessionId);
      setGeneratingBySession((prev) => ({ ...prev, [sessionId]: false }));
      setStatus("ready");
      // Баланс обязан обновиться после КАЖДОГО ответа модели (в т.ч. в режиме
      // ассистента): значение из ответа уже применено, а если его в теле не было
      // (сбой сборки, ошибка транспорта) — подтягиваем актуальный с бэкенда.
      if (chatAbortsRef.current[sessionId]?.controller === chatController)
        delete chatAbortsRef.current[sessionId];
      if (!balanceReceived) void refreshBalance();
    }
  }
  function handleStop() {
    const sid = activeSessionId;
    const active = chatAbortsRef.current[sid];
    if (!active) return;
    // 1) Бэкенд: выставляем событие отмены — цикл генерации прервётся между
    //    шагами, а долгий вызов LLM перестанет ожидаться (поток бросается).
    void fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: active.sessionId }),
      keepalive: true,
    }).catch(() => undefined);
    // 2) Разрываем ждущий fetch /api/chat: поздний ответ не будет показан.
    active.controller.abort();
    delete chatAbortsRef.current[sid];
    stopProgressPolling(sid);
    setGeneratingBySession((prev) => ({ ...prev, [sid]: false }));
    setStatus("ready");
    // Пометка в ленте той сессии, где шла генерация: ответ остановлен кнопкой.
    const stoppedMessage: Message = {
      id: `m${Date.now()}_stop`,
      role: "assistant",
      content: "⏹ Генерация остановлена.",
      timestamp: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    };
    setMessagesBySession((prev) => ({
      ...prev,
      [active.sessionId]: [...(prev[active.sessionId] ?? []), stoppedMessage],
    }));
  }

  return { input, attachments, status, isGenerating, liveThinking, liveReasoning, liveReplyBySession, setInputsBySession, handleAttachMany, handleRemoveAttachment, handleSend, handleStop };
}
