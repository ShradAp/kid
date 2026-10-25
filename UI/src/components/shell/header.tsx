"use client";

import { useState } from "react";
import { Menu, Coins, RefreshCw, NotebookPen, ScrollText, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { StatusPill } from "@/components/shell/status-pill";
import type { AssistantStatus } from "@/lib/assistant-data";

export function Header({
  onOpenDrawer,
  balance,
  tokensUsed,
  tokensTotal,
  status,
  onRefreshBalance,
  notesCount = 0,
  notesOpen = false,
  onToggleNotes,
  studioOpen = false,
  onToggleStudio,
  logsOpen = false,
  onToggleLogs,
}: {
  onOpenDrawer: () => void;
  balance: number;
  tokensUsed: number;
  tokensTotal: number;
  status: AssistantStatus;
  onRefreshBalance: () => Promise<void>;
  notesCount?: number;
  notesOpen?: boolean;
  onToggleNotes?: () => void;
  studioOpen?: boolean;
  onToggleStudio?: () => void;
  logsOpen?: boolean;
  onToggleLogs?: () => void;
}) {
  const [refreshing, setRefreshing] = useState(false);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await onRefreshBalance();
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center justify-between gap-3 border-b border-white/10 bg-background/80 px-4 backdrop-blur-md sm:px-6">
      <div className="flex items-center gap-1">
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "rounded-full text-foreground/80 hover:bg-white/10 hover:text-foreground",
                  logsOpen && "bg-primary/15 text-primary",
                )}
                onClick={onToggleLogs}
                aria-label="Toggle logs"
              />
            }
          >
            <ScrollText />
          </TooltipTrigger>
          <TooltipContent>Логи</TooltipContent>
        </Tooltip>

        <Button
          variant="ghost"
          size="icon"
          className="rounded-full text-foreground/80 hover:bg-white/10 hover:text-foreground"
          onClick={onOpenDrawer}
          aria-label="Open menu"
        >
          <Menu />
        </Button>
      </div>

      <div className="flex flex-1 items-center justify-center gap-2 overflow-x-auto sm:flex-none sm:justify-start">
        <div className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] py-1.5 pl-3 pr-1.5">
          <Coins className="size-3.5 text-amber-400" />
          <span className="text-xs font-medium text-foreground/90 text-nowrap">
            {balance.toLocaleString("ru-RU", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}{" "}
            ₽
          </span>
          <Tooltip>
            <TooltipTrigger
              render={
                <button
                  onClick={handleRefresh}
                  className="rounded-full p-1 text-muted-foreground transition hover:bg-white/10 hover:text-foreground"
                  aria-label="Refresh balance"
                >
                  <RefreshCw
                    className={cn("size-3", refreshing && "animate-spin")}
                  />
                </button>
              }
            />
            <TooltipContent>Refresh balance</TooltipContent>
          </Tooltip>
        </div>

        <div className="rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5">
          <span className="text-xs text-muted-foreground text-nowrap">
            {tokensUsed > 1000
              ? `${(tokensUsed / 1000).toFixed(1)}k`
              : tokensUsed}{" "}
            / 1M токенов
          </span>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <Tooltip>
          <TooltipTrigger
            render={
              <button
                onClick={onToggleStudio}
                aria-label="Открыть Document Studio"
                className={cn(
                  "rounded-full border border-white/10 bg-white/[0.04] p-2 text-foreground/80 transition hover:bg-white/10 hover:text-foreground",
                  studioOpen && "border-primary/50 bg-primary/15 text-primary",
                )}
              />
            }
          >
            <FileText className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>Document Studio</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger
            render={
              <button
                onClick={onToggleNotes}
                aria-label="Toggle notes"
                className={cn(
                  "relative flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1.5 text-foreground/80 transition hover:bg-white/10 hover:text-foreground",
                  notesOpen && "border-primary/50 bg-primary/15 text-primary",
                )}
              />
            }
          >
            <NotebookPen className="size-3.5" />
            {notesCount > 0 && (
              <span className="flex size-4 items-center justify-center rounded-full bg-primary text-[9px] font-bold text-primary-foreground">
                {notesCount > 9 ? "9+" : notesCount}
              </span>
            )}
          </TooltipTrigger>
          <TooltipContent>Заметки</TooltipContent>
        </Tooltip>

        <StatusPill status={status} />
      </div>
    </header>
  );
}
