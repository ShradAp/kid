"use client";

import type { ReactNode } from "react";
import { FileText, X } from "lucide-react";
import { ChatFeed } from "@/components/chat/chat-feed";
import { InputDeck, type Attachment } from "@/components/chat/input-deck";
import type { Message } from "@/lib/assistant-data";

/** Пресет, приложенный к чату (чипс над полем ввода). */
export interface PresetChip {
  id: string;
  name: string;
}

/**
 * Левая колонка студии: ровно тот же состав, что и в основном чате —
 * лента сообщений `ChatFeed` и поле ввода `InputDeck`. Дополнительно над
 * полем ввода показываются пресеты, приложенные к сообщению (их может быть
 * несколько — модель получает их и анализирует оформление).
 */
export function StudioChatConsole({
  messages,
  isGenerating,
  liveSteps,
  input,
  onInputChange,
  onSend,
  onStop,
  onClearChat,
  attachments,
  onAttachMany,
  onRemoveAttachment,
  presetChips = [],
  onRemovePreset,
  toolbar,
  fontSizeIndex = 1,
}: {
  messages: Message[];
  isGenerating: boolean;
  liveSteps: string[];
  input: string;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onClearChat: () => void;
  attachments: Attachment[];
  onAttachMany: (files: File[]) => void;
  onRemoveAttachment: (id: string) => void;
  presetChips?: PresetChip[];
  onRemovePreset?: (id: string) => void;
  /** Верхняя панель студии (профиль и список её чатов). */
  toolbar?: ReactNode;
  /** Размер шрифта ленты — та же настройка внешнего вида, что и в чате. */
  fontSizeIndex?: number;
}) {
  return (
    <section className="flex w-[32%] min-w-[320px] shrink-0 flex-col border-r border-white/10">
      {toolbar}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <ChatFeed
          messages={messages}
          isThinking={isGenerating}
          liveSteps={liveSteps}
          fontSizeIndex={fontSizeIndex}
        />
      </div>
      {presetChips.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-white/10 px-3 pt-2">
          {presetChips.map((chip) => (
            <span
              key={chip.id}
              className="flex items-center gap-1 rounded-lg border border-white/10 bg-card/90 py-1 pl-2 pr-1 text-xs text-foreground/80"
            >
              <FileText className="size-3.5 text-primary" />
              Пресет: {chip.name}
              {onRemovePreset && (
                <button
                  type="button"
                  className="rounded-full p-0.5 hover:bg-white/10"
                  onClick={() => onRemovePreset(chip.id)}
                  aria-label={`Убрать пресет ${chip.name}`}
                >
                  <X className="size-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      <InputDeck
        value={input}
        onChange={onInputChange}
        onSend={onSend}
        onStop={onStop}
        onClearChat={onClearChat}
        isGenerating={isGenerating}
        attachments={attachments}
        onAttachMany={onAttachMany}
        onRemoveAttachment={onRemoveAttachment}
      />
    </section>
  );
}
