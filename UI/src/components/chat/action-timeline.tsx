"use client";

import { Check, ChevronRight, FilePenLine, LoaderCircle, MessageSquare, Search, Terminal, Wrench } from "lucide-react";

export function ActionTimeline({ steps, reasoning, live = false, durationMs, interrupted = false }: {
  steps: string[]; reasoning?: string; live?: boolean; durationMs?: number; interrupted?: boolean;
}) {
  const seconds = typeof durationMs === "number" ? Math.max(0, Math.round(durationMs / 1000)) : null;
  return <details open={live} className="group mb-3 text-sm text-muted-foreground">
    <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg py-2 outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
      {live ? <LoaderCircle className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
      <span>{live ? "Выполняется работа" : interrupted ? "Работа остановлена" : "Работа завершена"}
        {steps.length > 0 && ` · событий: ${steps.length}`}
        {seconds !== null && ` · ${seconds >= 60 ? `${Math.floor(seconds / 60)} мин ${seconds % 60} с` : `${seconds} с`}`}
      </span>
      <ChevronRight className="ml-auto size-3.5 transition-transform group-open:rotate-90" />
    </summary>
    <div className="ml-1.5 space-y-3 border-l border-white/10 py-2 pl-4">
      {steps.map((step, index) => {
        const Icon = /^Команда:|^Вывод команды:/.test(step) ? Terminal : /файлов:|Правка|файл|документ/i.test(step) ? FilePenLine : /поиск|страниц/i.test(step) ? Search : /инструмент/i.test(step) ? Wrench : MessageSquare;
        const [title, ...body] = step.split("\n");
        return <div key={index} className="flex min-w-0 items-start gap-2">
          <Icon className="mt-0.5 size-3.5 shrink-0" />
          {body.length > 0 ? <details className="min-w-0 flex-1">
            <summary className="cursor-pointer break-words text-foreground/80 [overflow-wrap:anywhere]">{title}</summary>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-white/10 bg-white/[0.02] p-3 font-mono text-xs [overflow-wrap:anywhere]">{body.join("\n")}</pre>
          </details> : <span className="min-w-0 whitespace-pre-wrap text-foreground/80 [overflow-wrap:anywhere]">{title}</span>}
        </div>;
      })}
      {reasoning?.trim() && <details><summary className="cursor-pointer">Пояснения модели</summary><p className="mt-2 whitespace-pre-wrap">{reasoning}</p></details>}
      {live && steps.length === 0 && <p>Ожидание событий Codex…</p>}
    </div>
  </details>;
}
