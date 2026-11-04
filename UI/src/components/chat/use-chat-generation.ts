"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import type { Attachment } from "@/components/chat/input-deck";
import type { Message, AssistantStatus, Persona } from "@/lib/assistant-data";
import { ChatRequestQueue } from "@/lib/chat-request-queue";
import { playChatSound, unlockChatSound } from "@/lib/chat-notifications";
import { uploadAttachments } from "@/components/chat/attachment-upload";
import type { useChatSessions } from "@/components/chat/use-chat-sessions";
import type { useNotes } from "@/components/panels/use-notes";

type GenerationDependencies = Pick<ReturnType<typeof useChatSessions>,
  "activePersona" | "activeSessionId" | "personas" | "setMessagesBySession" | "setSessions" | "stateMutationRef" | "persistSession" | "setBalance" | "refreshBalance"
> & Pick<ReturnType<typeof useNotes>, "handleAddNote" | "setNotes"> & {
  logsOpen: boolean;
  fetchLogs: () => void;
  chatVisible?: boolean;
};

type PendingRequest = { id: string; text: string; attachments: Attachment[]; persona: Persona };

export function useChatGeneration({
  activePersona, activeSessionId, personas, setMessagesBySession, setSessions, stateMutationRef, persistSession, setBalance, refreshBalance, handleAddNote, setNotes, logsOpen, fetchLogs, chatVisible = true
}: GenerationDependencies) {
  const [liveReplyBySession, setLiveReplyBySession] = useState<Record<string, string>>({});
  // Ввод и вложения хранятся ПО СЕССИИ, чтобы текст в поле не перетекал между чатами.
  const [inputsBySession, setInputsBySession] = useState<Record<string, string>>({});
  const [attachmentsBySession, setAttachmentsBySession] = useState<
    Record<string, Attachment[]>
  >({});
  // Генерация отслеживается ПО СЕССИИ: можно отправить запрос в одном чате и,
  // пока он думает, отправить другой запрос в соседнем чате.
  const [generatingBySession, setGeneratingBySession] = useState<
    Record<string, boolean>
  >({});
  const activeSessionRef = useRef(activeSessionId);
  activeSessionRef.current = activeSessionId;
  const visibleRef = useRef(chatVisible);
  visibleRef.current = chatVisible;
  const personasRef = useRef(personas);
  personasRef.current = personas;
  const [unreadBySession, setUnreadBySession] = useState<Record<string, boolean>>({});
  const [steeringBySession, setSteeringBySession] = useState<Record<string, boolean>>({});
  const steerPending = useRef<Partial<Record<string, Promise<void>>>>({});
  const runRef = useRef<(sessionId: string, request: PendingRequest) => Promise<boolean>>(async () => false);
  const [requestQueue] = useState<ChatRequestQueue<PendingRequest>>(() => new ChatRequestQueue<PendingRequest>(
    (sid, job) => runRef.current(sid, job),
    () => setGeneratingBySession(Object.fromEntries(requestQueue.running().map((sid) => [sid, true]))),
  ));
  useEffect(() => {
    try {
      const saved: unknown = JSON.parse(localStorage.getItem("kid-unread-chats") || "{}");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) {
        setUnreadBySession(Object.fromEntries(Object.entries(saved).filter(([, value]) => value === true)));
      }
    } catch {}
  }, []);
  useEffect(() => {
    try { localStorage.setItem("kid-unread-chats", JSON.stringify(unreadBySession)); } catch {}
  }, [unreadBySession]);
  useEffect(() => {
    const markRead = () => {
      if (!visibleRef.current || document.visibilityState === "hidden" || !document.hasFocus()) return;
      setUnreadBySession((prev) => {
        if (!prev[activeSessionRef.current]) return prev;
        const next = { ...prev }; delete next[activeSessionRef.current]; return next;
      });
    };
    markRead();
    window.addEventListener("focus", markRead);
    document.addEventListener("visibilitychange", markRead);
    return () => { window.removeEventListener("focus", markRead); document.removeEventListener("visibilitychange", markRead); };
  }, [activeSessionId, chatVisible]);
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
    Record<string, { controller: AbortController; sessionId: string; operationId: string; personaId: string }>
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
  function handleSend() {
    if (!input.trim() && attachments.length === 0) return;
    if (!activePersona) { toast.error("Профили ещё не загружены"); return; }
    unlockChatSound();
    const job = { id: crypto.randomUUID(), text: input, attachments, persona: activePersona };
    setInputsBySession((prev) => ({ ...prev, [activeSessionId]: "" }));
    setAttachmentsBySession((prev) => ({ ...prev, [activeSessionId]: [] }));
    requestQueue.enqueue(activeSessionId, job);
  }

  async function sendRequest(sessionId: string, job: PendingRequest): Promise<boolean> {
    const activePersona = personasRef.current.find((persona) => persona.id === job.persona.id);
    if (!activePersona) { toast.error("Профиль запроса удалён"); return false; }
    // Фиксируем ввод/вложения на момент отправки: даже если пользователь
    // переключится на другой чат во время загрузки файлов, текст уйдёт в нужную сессию.
    const sessionInput = job.text;
    const sessionAttachments = job.attachments;
    if (!sessionInput.trim() && sessionAttachments.length === 0) return false;
    // Профили обязаны быть загружены: без них нет ни модели, ни ключа,
    // ни системного промпта — запрос ушёл бы «в никуда».
    if (personas.length === 0) {
      toast.error("Профили ещё не загружены — подождите пару секунд");
      return false;
    }
    const chatController = new AbortController();
    chatAbortsRef.current[sessionId] = { controller: chatController, sessionId, operationId: job.id, personaId: activePersona.id };
    // The chat endpoint restores the protected connection before submitting a
    // turn. A cached status must not prevent that reconnect from ever running.
    const { accumulatedFilesPrefix, payloadAttachments, sentAttachments } =
      await uploadAttachments(sessionAttachments, activePersona);
    if (chatController.signal.aborted) {
      if (chatAbortsRef.current[sessionId]?.controller === chatController) delete chatAbortsRef.current[sessionId];
      return false;
    }
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
    setGeneratingBySession((prev) => ({ ...prev, [sessionId]: true }));
    startProgressPolling(sessionId);
    // Контроллер отмены: кнопка «стоп» разорвёт этот fetch и сообщит бэкенду.
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
        operation_id: job.id,
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
      if (chatController.signal.aborted) return false;
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
      await steerPending.current[sessionId];
      if (chatController.signal.aborted) return false;
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
      stateMutationRef.current += 1;
      if (!data.cancelled) {
        playChatSound();
        if (!visibleRef.current || activeSessionRef.current !== sessionId || document.visibilityState === "hidden" || !document.hasFocus()) {
          setUnreadBySession((prev) => ({ ...prev, [sessionId]: true }));
        }
      }
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
        void fetch("/api/notes").then((response) => response.json()).then((notesData) => {
          if (Array.isArray(notesData.notes)) setNotes(notesData.notes);
        }).catch(() => toast.error("Не удалось обновить список заметок"));
      }
      return !data.cancelled;
    } catch (err) {
      // «Стоп» — штатная отмена: не пушим ошибку в ленту и не показываем тост.
      if (chatController.signal.aborted) return false;
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
      return false;
    } finally {
      stopProgressPolling(sessionId);
      setGeneratingBySession((prev) => ({ ...prev, [sessionId]: false }));
      // Баланс обязан обновиться после КАЖДОГО ответа модели (в т.ч. в режиме
      // ассистента): значение из ответа уже применено, а если его в теле не было
      // (сбой сборки, ошибка транспорта) — подтягиваем актуальный с бэкенда.
      if (chatAbortsRef.current[sessionId]?.controller === chatController)
        delete chatAbortsRef.current[sessionId];
      if (!balanceReceived) void refreshBalance();
    }
  }
  runRef.current = sendRequest;

  async function handleSteer() {
    const sessionId = activeSessionId;
    const active = chatAbortsRef.current[sessionId];
    if (!active || steerPending.current[sessionId] || (!input.trim() && !attachments.length)) return;
    const text = input;
    const files = attachments;
    setSteeringBySession((prev) => ({ ...prev, [sessionId]: true }));
    const task = (async () => {
      try {
        const uploaded = await uploadAttachments(files, activePersona);
        const response = await fetch("/api/chat/steer", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, persona_id: active.personaId,
            operation_id: active.operationId, prompt: uploaded.accumulatedFilesPrefix + text,
            attachments: uploaded.payloadAttachments }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Уточнение не принято");
        stateMutationRef.current += 1;
        setMessagesBySession((prev) => ({ ...prev, [sessionId]: [...(prev[sessionId] ?? []), {
          id: crypto.randomUUID(), role: "user", content: text || "Прикреплены файлы",
          timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
          attachments: uploaded.sentAttachments,
        }] }));
        setInputsBySession((prev) => ({ ...prev, [sessionId]: prev[sessionId] === text ? "" : prev[sessionId] }));
        setAttachmentsBySession((prev) => ({ ...prev, [sessionId]: (prev[sessionId] ?? []).filter((file) => !files.includes(file)) }));
        toast.success("Уточнение передано в текущую задачу");
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "Не удалось подтвердить уточнение. Автоматический повтор не выполняется");
      } finally {
        delete steerPending.current[sessionId];
        setSteeringBySession((prev) => ({ ...prev, [sessionId]: false }));
      }
    })();
    steerPending.current[sessionId] = task;
    await task;
  }

  function discardSession(sessionId: string) {
    requestQueue.clear(sessionId);
    if (chatAbortsRef.current[sessionId]) {
      void fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }), keepalive: true }).catch(() => undefined);
    }
    chatAbortsRef.current[sessionId]?.controller.abort();
    stopProgressPolling(sessionId);
    setUnreadBySession((prev) => { const next = { ...prev }; delete next[sessionId]; return next; });
  }
  function handleStop() {
    const sid = activeSessionId;
    requestQueue.clear(sid);
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

  return { input, attachments, status: (isGenerating ? "thinking" : "ready") as AssistantStatus, isGenerating, generatingBySession, unreadBySession,
    steering: Boolean(steeringBySession[activeSessionId]), canSteer: activePersona?.engine === "codex",
    pendingRequests: requestQueue.items(activeSessionId),
    removeQueued: (index: number) => requestQueue.remove(activeSessionId, index),
    startQueued: () => requestQueue.start(activeSessionId), discardSession,
    liveThinking, liveReasoning, liveReplyBySession, setInputsBySession, handleAttachMany, handleRemoveAttachment, handleSend, handleSteer, handleStop };
}
