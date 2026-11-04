"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { personas as initialPersonas, type Persona, type Message } from "@/lib/assistant-data";

// Локальная копия последнего открытого чата — fallback на случай,
// если бэкенд (например, ручной server_bridge) не хранит active_session.
const ACTIVE_SESSION_STORAGE_KEY = "kid-active-session";

export function useChatSessions({ closeDrawer, openProfiles }: { closeDrawer: () => void; openProfiles: () => void }) {
  // Профили приходят ТОЛЬКО с бэкенда (/api/init). Демо-персоны убраны:
  // пока состояние не загружено, интерфейс не должен показывать фиктивные
  // профили и создавать с их именем чаты.
  const [personas, setPersonas] = useState<Persona[]>(initialPersonas);
  const [activePersonaId, setActivePersonaId] = useState("main");
  const [sessions, setSessions] = useState([
    {
      id: "default",
      personaId: "main",
      title: "Основной диалог",
      timestamp: "Сейчас",
    },
  ]);
  const [activeSessionId, setActiveSessionId] = useState("default");
  const [messagesBySession, setMessagesBySession] = useState<
    Record<string, Message[]>
  >({
    default: [],
  });
  // Версия локальных изменений защищает чат от запоздалого /api/init,
  
  // который иначе может вернуть старую историю поверх только что полученного ответа.
  const stateMutationRef = useRef(0);
  // Инициализация из бэкенда завершена: только после этого можно запоминать
  // активный чат, иначе стартовый "default" перетёр бы сохранённый чат.
  const stateLoadedRef = useRef(false);
  // Счётчик авто-ретраев /api/init: на старте приложения мост может ещё
  // не слушать порт — повторяем запрос, а не остаёмся с пустым состоянием.
  const fetchRetriesRef = useRef(0);
  // Баланс и токены
  const [balance, setBalance] = useState(0);
  // Принудительное обновление баланса (кнопка Refresh в шапке): лёгкий эндпоинт,
  // который игнорирует TTL-кэш и сразу идёт в SprutDock за актуальным значением.
  const activePersona =
    personas.find((p) => p.id === activePersonaId) ?? personas[0];

  const refreshBalance = async () => {
    if (activePersona?.engine === "codex") return;
    try {
      const response = await fetch("/api/balance/refresh");
      const data = await response.json();
      if (typeof data.balance === "number") {
        setBalance(data.balance);
      }
    } catch {
      // нет связи с бэкендом — оставляем прежнее значение
    }
  };
  function persistSession(id: string, personaId: string, title: string) {
    fetch("/api/session/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, personaId, title }),
    }).catch(() => {});
  }
  // Запоминает последний открытый чат: на бэкенде (переживает рестарт приложения)
  // и локально (fallback, если бэкенд недоступен или не поддерживает activate).
  function persistActiveSession(id: string) {
    try {
      window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, id);
    } catch {
      // хранилище может быть недоступно (приватный режим и т.п.)
    }
    fetch("/api/session/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: id }),
    }).catch(() => {});
  }
  // Смена активного чата (клик в сайдбаре, новый чат, восстановление после
  // рестарта) — сразу фиксируется, чтобы открыться при следующем запуске.
  useEffect(() => {
    if (!stateLoadedRef.current || !activeSessionId) return;
    persistActiveSession(activeSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);
  // Загрузка состояния из Python (профили + ВСЕ сессии всех профилей)
  const fetchState = () => {
    const requestedMutationVersion = stateMutationRef.current;
    fetch("/api/init")
      .then((r) => r.json())
      .then((data) => {
        // Не применяем устаревший снимок, если за время запроса пользователь
        // отправил/удалил/очистил чат локально.
        if (requestedMutationVersion !== stateMutationRef.current) return;
        if (data.personas && data.personas.length > 0) {
          setPersonas(data.personas);
          if (data.active_profile) setActivePersonaId(data.active_profile);
        }
        if (typeof data.balance === "number") {
          setBalance(data.balance);
        }
        // Новый формат: sessions + session_messages (все чаты всех профилей)
        if (Array.isArray(data.sessions) && data.sessions.length > 0) {
          const restoredSessions = data.sessions.map((s: any) => ({
            id: String(s.id),
            personaId: String(s.personaId || data.active_profile || "main"),
            title: String(s.title || "Новый чат"),
            timestamp: String(s.timestamp || "Ранее"),
          }));
          setSessions(restoredSessions);
          const msgs: Record<string, Message[]> = {};
          const sm = data.session_messages || {};
          for (const s of restoredSessions) {
            const arr = Array.isArray(sm[s.id]) ? sm[s.id] : [];
            msgs[s.id] = arr
              .filter((m: any) => m.role === "user" || m.role === "assistant")
              .map((m: any, idx: number) => ({
                id: `hist_${s.id}_${idx}`,
                role: m.role,
                content:
                  typeof m.content === "string"
                    ? m.content
                    : JSON.stringify(m.content),
                timestamp: "Ранее",
                attachments: Array.isArray(m.attachments)
                  ? m.attachments
                      .filter((a: any) => a && typeof a.name === "string" && typeof a.url === "string")
                      .map((a: any) => ({ name: a.name, url: a.url }))
                  : undefined,
                reasoning: typeof m.reasoning === "string" ? m.reasoning : undefined,
                activity: m.activity || (m.codex_operation ? {} : undefined),
                images: Array.isArray(m.images) ? m.images : undefined,
                thinking: Array.isArray(m.thinking) ? m.thinking : undefined,
              }));
          }
          setMessagesBySession(msgs);
          const activePid = String(data.active_profile || "main");
          // Восстановление последнего открытого чата после рестарта приложения:
          // приоритет — сервер (config.json), затем локальный кэш клиента.
          const isFirstLoad = !stateLoadedRef.current;
          let savedSessionId: string | null =
            isFirstLoad && typeof data.active_session === "string" && data.active_session
              ? data.active_session
              : null;
          if (isFirstLoad && !savedSessionId) {
            try {
              savedSessionId = window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
            } catch {
              savedSessionId = null;
            }
          }
          const savedSession = savedSessionId
            ? restoredSessions.find((s: any) => s.id === savedSessionId)
            : undefined;
          if (savedSession) {
            // Чат мог принадлежать другому профилю — открываем и его профиль.
            if (savedSession.personaId && savedSession.personaId !== activePid) {
              setActivePersonaId(savedSession.personaId);
              fetch("/api/set_active_profile", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ profile_id: savedSession.personaId }),
              }).catch(() => {});
            }
            setActiveSessionId(savedSession.id);
          } else {
            setActiveSessionId((prev) => {
              const cur = restoredSessions.find((s: any) => s.id === prev);
              if (cur && cur.personaId === activePid) return prev;
              const firstOfActive = restoredSessions.find(
                (s: any) => s.personaId === activePid,
              );
              if (firstOfActive) return firstOfActive.id;
              if (restoredSessions.some((s: any) => s.id === "default"))
                return "default";
              return restoredSessions[0].id;
            });
          }
        } else if (
          data.history &&
          Array.isArray(data.history) &&
          data.history.length > 0
        ) {
          // Fallback для старого бэкенда: только default
          const restoredMessages: Message[] = data.history
            .filter((m: any) => m.role === "user" || m.role === "assistant")
            .map((m: any, idx: number) => ({
              id: `hist_${idx}_${Date.now()}`,
              role: m.role,
              content:
                typeof m.content === "string"
                  ? m.content
                  : JSON.stringify(m.content),
              timestamp: "Ранее",
            }));
          setMessagesBySession((prev) => ({
            ...prev,
            default: restoredMessages,
          }));
        }
        // Инициализация завершена — дальше можно запоминать активный чат.
        stateLoadedRef.current = true;
        fetchRetriesRef.current = 0;
      })
      .catch(() => {
        // Мост мог ещё не подняться (холодный старт приложения) — повторяем
        // /api/init, вместо вечного показа пустого состояния.
        if (fetchRetriesRef.current < 5) {
          fetchRetriesRef.current += 1;
          window.setTimeout(fetchState, 2000);
          return;
        }
        // Бэкенд так и не ответил — разрешаем локальное запоминание выбора.
        stateLoadedRef.current = true;
      });
  };
  useEffect(() => {
    fetchState();
  }, []);
  function handleSelectPersona(id: string) {
    setActivePersonaId(id);
    // Уведомляем бэкенд о смене активного профиля
    fetch("/api/set_active_profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: id }),
    }).catch(() => {});
  
    const firstSessionForPersona = sessions.find(
      (session) => session.personaId === id,
    );
    if (firstSessionForPersona) {
      setActiveSessionId(firstSessionForPersona.id);
    } else {
      handleNewChat(id);
    }
  }
  function handleSelectSession(id: string) {
    const session = sessions.find((item) => item.id === id);
    if (session && session.personaId !== activePersonaId) {
      setActivePersonaId(session.personaId);
      void fetch("/api/set_active_profile", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: session.personaId }) }).catch(() => undefined);
    }
    setActiveSessionId(id);
    closeDrawer();
  }
  function handleNewChat(personaId: string = activePersonaId) {
    // Чат можно создавать только под СУЩЕСТВУЮЩИЙ профиль: пока профили
    // не загружены с бэкенда, создание привязало бы чат к фиктивной персоне.
    if (!personas.some((p) => p.id === personaId)) {
      toast.error("Профили ещё не загружены — подождите пару секунд");
      return;
    }
    // Новый чат под другой персоной: сначала переключаемся на неё, иначе чат
    // закрепился бы за текущим активным профилем, а не за выбранной персоной.
    if (personaId !== activePersonaId) {
      setActivePersonaId(personaId);
      fetch("/api/set_active_profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: personaId }),
      }).catch(() => {});
    }
    stateMutationRef.current += 1;
    const id = `s${Date.now()}`;
    setSessions((prev) => [
      { id, personaId, title: "Новый чат", timestamp: "Только что" },
      ...prev,
    ]);
    setMessagesBySession((prev) => ({ ...prev, [id]: [] }));
    setActiveSessionId(id);
    closeDrawer();
    persistSession(id, personaId, "Новый чат");
  }
  function handleClearChat() {
    stateMutationRef.current += 1;
    const sid = activeSessionId;
    stateMutationRef.current += 1;
    setMessagesBySession((prev) => ({ ...prev, [sid]: [] }));
    fetch("/api/session/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid }),
    }).catch(() => {});
    toast.success("Чат очищен");
  }
  async function handleDeleteSession(id: string) {
    stateMutationRef.current += 1;
    const remaining = sessions.filter((s) => s.id !== id);
    setSessions(remaining);
    setMessagesBySession((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    if (activeSessionId === id) {
      const fallback =
        remaining.find((s) => s.personaId === activePersonaId) ?? remaining[0];
      if (fallback) {
        setActiveSessionId(fallback.id);
      } else {
        // Удалили последний чат: сразу создаём новый, иначе активной
        // осталась бы несуществующая сессия, куда нельзя писать.
        handleNewChat();
      }
    }
  
    // Чат должен быть стёрт НАМЕРТВО на бэкенде (файлы + кэш памяти + мета),
    // иначе он воскреснет при перезагрузке или после ответа ассистента.
    try {
      const res = await fetch("/api/session/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.ok !== true) {
        throw new Error(
          typeof data?.error === "string" && data.error ? data.error : `HTTP ${res.status}`,
        );
      }
      toast.success("Чат удалён");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "нет связи с сервером Python";
      toast.error(`Чат не удалён на сервере: ${message}`);
      // Сервер не подтвердил удаление — возвращаем честное состояние списка.
      stateMutationRef.current += 1;
      fetchState();
    }
  }
  async function handleDeletePersona(id: string) {
    if (personas.length <= 1) {
      toast.error("Нельзя удалить единственный профиль");
      return false;
    }
    try {
      const res = await fetch("/api/delete_persona", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile_id: id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const remaining = personas.filter((item) => item.id !== id);
      setPersonas(remaining);
      if (activePersonaId === id) handleSelectPersona(String(data.active_profile || remaining[0].id));
      toast.success("Профиль удалён");
      return true;
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Не удалось удалить профиль");
      return false;
    }
  }
  function handleCreatePersona() {
    const id = `profile_${Date.now()}`;
    const newPersona: Persona = {
      id,
      emoji: "✨",
      name: "Новый профиль",
      description: "Пользовательский профиль ассистента.",
      systemPrompt: "",
      temperature: 0.7,
      baseUrl: "",
      apiKey: "",
      modelName: "",
    };
    setPersonas((prev) => [...prev, newPersona]);
    setActivePersonaId(id);
    openProfiles();
    closeDrawer();
  }
  async function handleSavePersona(updated: Persona) {
    // Сохранять можно только профиль, реально пришедший с бэкенда: иначе
    // устаревшая/демо-копия персоны затёрла бы настоящие настройки.
    if (!personas.some((p) => p.id === updated.id)) {
      throw new Error("Профиль не загружен — сохранение отменено");
    }
    try {
      window.localStorage.removeItem("kid-personas");
    } catch {
      // localStorage may be unavailable in restricted WebViews
    }
  
    // Сохраняем на бэкенд, чтобы изменения пережили перезапуск
    try {
      const res = await fetch("/api/save_personas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona: updated }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(`Ошибка сохранения профиля: ${err.error ?? res.status}`);
      }
      const saved: Persona = { ...updated, apiKey: "", clearApiKey: false };
      setPersonas((prev) => prev.map((p) => (p.id === updated.id ? saved : p)));
    } catch (error) {
      throw error instanceof Error ? error : new Error("Нет связи с сервером — профиль не сохранён");
    }
  }

  return {
    activePersona,
    personas,
    activePersonaId,
    sessions,
    setSessions,
    activeSessionId,
    messagesBySession,
    setMessagesBySession,
    stateMutationRef,
    balance,
    setBalance,
    fetchState,
    refreshBalance,
    persistSession,
    handleSelectPersona,
    handleSelectSession,
    handleNewChat,
    handleClearChat,
    handleDeleteSession,
    handleDeletePersona,
    handleCreatePersona,
    handleSavePersona,
  };
}
