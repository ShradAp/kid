"use client";

import { useEffect, useRef } from "react";
import { RefreshCw, ScrollText, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export function LogsPanel({
  open,
  logs,
  onClose,
  onRefresh,
}: {
  open: boolean;
  logs: string[];
  onClose: () => void;
  onRefresh: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // true, пока пользователь стоит на дне лога: новые записи дотягивают
  // ленту вниз, а ручная прокрутка вверх автоскролл отключает.
  const followBottomRef = useRef(true);

  useEffect(() => {
    if (!open) {
      // при следующем открытии панель снова покажется на последней строке
      followBottomRef.current = true;
      return;
    }
    const container = scrollRef.current;
    if (!container) return;
    // Логи открываются сразу со скроллом на нижней строке
    container.scrollTop = container.scrollHeight;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const container = scrollRef.current;
    if (!container) return;
    const updatePosition = () => {
      followBottomRef.current =
        container.scrollHeight - container.clientHeight <= container.scrollTop + 50;
    };
    container.addEventListener("scroll", updatePosition, { passive: true });
    return () => container.removeEventListener("scroll", updatePosition);
  }, [open]);

  useEffect(() => {
    if (!open || !followBottomRef.current) return;
    const container = scrollRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [open, logs]);

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <SheetContent
        side="left"
        showCloseButton={false}
        className="w-full max-w-none gap-0 border-r border-border/60 bg-zinc-900/95 p-0 backdrop-blur-md sm:max-w-none"
      >
        <SheetHeader className="flex h-16 shrink-0 flex-row items-center justify-between gap-2 border-b border-white/10 px-4 py-3 sm:px-6">
          <SheetTitle className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <ScrollText className="size-4 text-primary" />
            <span>Логи</span>
            {logs.length > 0 && (
              <span className="rounded-full bg-white/10 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                {logs.length}
              </span>
            )}
          </SheetTitle>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full text-muted-foreground hover:bg-white/10 hover:text-foreground"
              onClick={onRefresh}
              aria-label="Refresh logs"
            >
              <RefreshCw className="size-3.5" />
            </Button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full p-1 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
              aria-label="Close logs"
            >
              <X className="size-4" />
            </button>
          </div>
        </SheetHeader>

        <div
          ref={scrollRef}
          className="min-h-0 flex-1 overflow-y-auto bg-black/40 px-4 py-3 sm:px-6"
        >
          {logs.length === 0 ? (
            <div className="px-2 py-6 text-center text-xs text-muted-foreground">
              Пока нет записей в логе.
            </div>
          ) : (
            <div className="font-mono text-[11px] leading-relaxed text-emerald-300/90 [overflow-wrap:anywhere] [word-break:normal]">
              {/* Показываем ВСЕ записи целиком — логи не должны скрываться,
                  панель сразу открывается прокрученной до нижней строки. */}
              {logs.map((line, index) => {
                const match = line.match(/^(.*?)(\b\d{2}:\d{2}:\d{2}\b)(.*)$/);
                return (
                  <div key={`${index}-${line}`} className="flex items-start gap-1.5">
                    {match ? (
                      <>
                        <span className="shrink-0 text-zinc-500 [font-variant-numeric:tabular-nums]">
                          {match[1]}{match[2]}
                        </span>
                        <span className="min-w-0">{match[3]}</span>
                      </>
                    ) : (
                      <span>{line}</span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
