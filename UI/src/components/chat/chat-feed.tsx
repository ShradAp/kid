"use client";

import { useEffect, useRef, type RefObject } from "react";
import { Sparkles } from "lucide-react";
import { ChatMessage } from "@/components/chat/chat-message";
import { ThinkingAccordion } from "@/components/chat/thinking-accordion";
import type { Message } from "@/lib/assistant-data";

const FEED_SPACING_CLASSES = [
  "gap-5 py-5",
  "gap-6 py-6",
  "gap-7 py-7",
  "gap-8 py-8",
];

// Насколько близко (в пикселях) к нижнему краю ленты нужно находиться, чтобы
// автопрокрутка продолжала «прилипать» к низу. Пока пользователь прокрутил
// вверх дальше этого порога — лента не дёргается при росте контента.
const STICK_TO_BOTTOM_THRESHOLD_PX = 80;

export function ChatFeed({
  messages,
  isThinking,
  liveSteps,
  liveReasoning,
  fontSizeIndex,
  scrollContainerRef,
}: {
  messages: Message[];
  isThinking: boolean;
  /** Живые шаги генерации (GET /api/progress): вызовы инструментов в реальном времени */
  liveSteps: string[];
  /** Живой текст размышлений модели (reasoning content) в реальном времени */
  liveReasoning?: string;
  fontSizeIndex: number;
  /** Скролл-контейнер ленты (<main> в page.tsx): нужен для «прилипания» к низу. */
  scrollContainerRef?: RefObject<HTMLElement | null>;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const prevMessagesRef = useRef<Message[]>(messages);

  // Следим, у нижнего ли края ленты находится пользователь. Как только он
  // прокрутил вверх — выключаем автопрокрутку; вернулся вниз — включаем снова.
  useEffect(() => {
    const el = scrollContainerRef?.current;
    if (!el) return;
    const handleScroll = () => {
      stickToBottomRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight <
        STICK_TO_BOTTOM_THRESHOLD_PX;
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    handleScroll();
    return () => el.removeEventListener("scroll", handleScroll);
  }, [scrollContainerRef]);

  // Автопрокрутка к низу по мере роста ленты. Новое сообщение или переключение
  // сессии следуют вниз безусловно; поток «размышлений» (liveReasoning) и шаги —
  // только пока пользователь сам находится у нижнего края.
  useEffect(() => {
    if (prevMessagesRef.current !== messages) {
      prevMessagesRef.current = messages;
      stickToBottomRef.current = true;
    }
    if (!stickToBottomRef.current) return;
    const el = scrollContainerRef?.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    } else {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isThinking, liveSteps.length, liveReasoning, scrollContainerRef]);

  if (messages.length === 0 && !isThinking) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
        <div className="flex size-12 items-center justify-center rounded-2xl border border-white/10 bg-white/[0.04]">
          <Sparkles className="size-5 text-primary" />
        </div>
        <p className="text-sm text-muted-foreground">
          Start a new conversation with your assistant.
        </p>
      </div>
    );
  }

  return (
    <div
      className={`mx-auto flex w-full max-w-4xl flex-1 flex-col px-4 sm:px-6 ${FEED_SPACING_CLASSES[fontSizeIndex]}`}
      style={{ fontSize: "var(--chat-font-size)" }}
    >
      {messages.map((message) => (
        <ChatMessage key={message.id} message={message} />
      ))}
      {isThinking && (
        <div className="flex max-w-[85%] flex-col gap-1">
          {liveSteps.length > 0 || (liveReasoning && liveReasoning.trim()) ? (
            <ThinkingAccordion steps={liveSteps} reasoning={liveReasoning} live />
          ) : (
            <div className="flex items-center gap-1.5 rounded-2xl border border-white/10 bg-card px-4 py-3">
              <span className="size-1.5 animate-bounce rounded-full bg-primary [animation-delay:-0.3s]" />
              <span className="size-1.5 animate-bounce rounded-full bg-primary [animation-delay:-0.15s]" />
              <span className="size-1.5 animate-bounce rounded-full bg-primary" />
            </div>
          )}
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
