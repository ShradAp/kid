import { useState } from "react";
import { Brain } from "lucide-react";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";

// Состояние сворачивания/разворачивания блока «Thinking process» хранится
// локально, чтобы оно переживало перезапуск приложения, переключение диалога
// и смену профиля (React-состояние при этом сбрасывается).
const THINKING_EXPANDED_KEY = "kid-thinking-expanded";

function readExpandedPref(): boolean {
  try {
    return window.localStorage.getItem(THINKING_EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

export function ThinkingAccordion({
  steps,
  reasoning,
  live = false,
}: {
  steps: string[];
  /** Связный текст «размышлений» модели (reasoning content / thought parts). */
  reasoning?: string;
  /** live: данные приходят в реальном времени из GET /api/progress во время генерации */
  live?: boolean;
}) {
  const [expanded, setExpanded] = useState<boolean>(() => live || readExpandedPref());

  const hasReasoning = Boolean(reasoning && reasoning.trim());
  const hasSteps = steps.length > 0;
  // Live-блок во время генерации всегда развёрнут (мысли льются в реальном времени).
  const open = live ? true : expanded;

  function handleValueChange(value: string[]) {
    const next = value.length > 0;
    setExpanded(next);
    if (!live) {
      try {
        window.localStorage.setItem(THINKING_EXPANDED_KEY, next ? "1" : "0");
      } catch {
        // localStorage может быть недоступен в ограниченном WebView
      }
    }
  }

  return (
    <Accordion
      className="mb-3 w-full"
      value={open ? ["thinking"] : []}
      onValueChange={handleValueChange}
    >
      <AccordionItem
        value="thinking"
        className={`rounded-lg border px-3 ${
          live
            ? "border-primary/25 bg-primary/[0.04]"
            : "border-white/10 bg-white/[0.02]"
        }`}
      >
        <AccordionTrigger className="py-2.5 text-[0.8em] text-muted-foreground hover:no-underline [&>svg]:text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <Brain
              className={`size-3.5 text-primary ${live ? "animate-pulse" : ""}`}
              data-icon="inline-start"
            />
            Thinking process
          </span>
        </AccordionTrigger>
        <AccordionContent>
          <div className="flex flex-col gap-2 border-t border-white/10 pt-2.5 pb-1 text-[0.8em] text-muted-foreground">
            {hasReasoning && (
              <div
                className={`whitespace-pre-wrap ${
                  live ? "animate-pulse text-foreground/90" : "text-foreground/80"
                }`}
              >
                {reasoning}
              </div>
            )}
            {hasSteps && (
              <ol className="flex flex-col gap-1.5">
                {steps.map((step, i) => (
                  <li
                    key={i}
                    className={`flex gap-2 ${
                      live && i === steps.length - 1
                        ? "animate-pulse text-foreground/90"
                        : ""
                    }`}
                  >
                    <span className="text-primary/70">{i + 1}.</span>
                    <span>{step}</span>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
}
