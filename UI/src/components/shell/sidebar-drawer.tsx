"use client";

import { useEffect, useRef, useState } from "react";
import { Settings, Pin, Trash2, Plus, MessageSquare, PenTool } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ChatSession, Persona } from "@/lib/assistant-data";
import { profileColor } from "@/lib/profile-colors";

export function SidebarDrawer({
  open,
  onOpenChange,
  personas,
  activePersonaId,
  onSelectPersona,
  onEditProfiles,
  onCreatePersona,
  onDeletePersona,
  sessions,
  allSessions,
  activeSessionId,
  onSelectSession,
  onNewChat,
  onDeleteSession,
  onOpenSettings,
  useProfileAccents = true,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  personas: Persona[];
  activePersonaId: string;
  onSelectPersona: (id: string) => void;
  onEditProfiles: () => void;
  onCreatePersona: () => void;
  onDeletePersona: (id: string) => void;
  sessions: ChatSession[];
  allSessions: ChatSession[];
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onNewChat: (personaId?: string) => void;
  onDeleteSession?: (id: string) => void;
  onOpenSettings: () => void;
  useProfileAccents?: boolean;
}) {
  const activePersona = personas.find((p) => p.id === activePersonaId);
  const [chatMenuPersonaId, setChatMenuPersonaId] = useState<string | null>(null);
  const [deletePersonaId, setDeletePersonaId] = useState<string | null>(null);
  const chatMenuRefs = useRef<Record<string, HTMLDivElement | null>>({});

  useEffect(() => {
    if (chatMenuPersonaId === null) return;
    function handleOutsideClick(event: MouseEvent) {
      const activeMenuId: string | null = chatMenuPersonaId;
      if (activeMenuId === null) return;
      const menuRef = chatMenuRefs.current[activeMenuId];
      if (menuRef && !menuRef.contains(event.target as Node)) {
        setChatMenuPersonaId(null);
      }
    }
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, [chatMenuPersonaId]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="left"
        className="flex w-80 flex-col gap-0 border-white/10 bg-sidebar p-0"
      >
        <SheetHeader className="border-b border-white/10 px-4 py-4 pr-12">
          <SheetTitle className="sr-only">Assistant menu</SheetTitle>
          <div className="flex flex-col items-stretch gap-1">
            {personas.map((persona, index) => {
              const accent = profileColor(persona.color, index).hex;
              const active = persona.id === activePersonaId;
              return <div key={persona.id} role="group" aria-label={`Профиль ${persona.name}`}
                data-profile-id={persona.id} data-active={active}
                className={cn("flex items-center gap-0.5", useProfileAccents ? "w-fit rounded-full border border-transparent p-1 transition-colors" : "w-full")}
                style={useProfileAccents && active ? { borderColor: `${accent}80`, backgroundColor: `${accent}12` } : undefined}>
                <Tooltip>
                  <TooltipTrigger render={<Button variant="ghost" size="icon" className={cn("size-11 rounded-full bg-primary/20 text-primary", !useProfileAccents && active && "ring-2 ring-primary")} style={useProfileAccents ? { color: "#211D25", backgroundColor: accent } : undefined} onClick={() => onSelectPersona(persona.id)} onContextMenu={(event) => { event.preventDefault(); setDeletePersonaId(persona.id); }} aria-label={persona.name} aria-current={active ? "true" : undefined} /> }>
                    <span className="text-base font-semibold">{(persona.name ?? "?").charAt(0).toUpperCase()}</span>
                  </TooltipTrigger>
                  <TooltipContent>{persona.name}</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger render={<Button variant="ghost" size="icon" className={useProfileAccents ? "size-11 rounded-full" : undefined} onClick={() => { onSelectPersona(persona.id); onEditProfiles(); }} aria-label={`Настройки профиля ${persona.name}`} />}>
                    <Settings className="size-5" />
                  </TooltipTrigger>
                  <TooltipContent>Настройки профиля</TooltipContent>
                </Tooltip>
                <div ref={(element) => { chatMenuRefs.current[persona.id] = element; }} className="relative">
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn("size-11", useProfileAccents && "rounded-full")}
                    aria-label="Открыть список чатов"
                    title="Чаты"
                    aria-expanded={chatMenuPersonaId === persona.id}
                    onClick={() => setChatMenuPersonaId((current) => current === persona.id ? null : persona.id)}
                  >
                    <MessageSquare className="size-5" />
                  </Button>
                  {chatMenuPersonaId === persona.id && (
                    <div className="absolute left-0 top-full z-50 mt-2 w-72 rounded-lg border border-white/10 bg-popover p-1 text-popover-foreground shadow-md">
                      <div className="px-2 py-1.5 text-sm font-semibold">Чаты профиля</div>
                      <div className="my-1 h-px bg-white/10" />
                      {allSessions.filter((session) => session.personaId === persona.id).length === 0 ? (
                        <div className="px-2 py-2 text-sm text-muted-foreground">
                          Нет активных чатов
                        </div>
                      ) : (
                        allSessions.filter((session) => session.personaId === persona.id).map((session) => (
                          <button
                            key={session.id}
                            type="button"
                            onClick={() => {
                              onSelectSession(session.id);
                              setChatMenuPersonaId(null);
                            }}
                            className="flex w-full flex-col items-start gap-0.5 rounded-sm px-2 py-1.5 text-left text-sm outline-none transition hover:bg-accent hover:text-accent-foreground"
                          >
                            <span className="w-full truncate font-medium">{session.title}</span>
                            <span className="w-full truncate text-xs text-muted-foreground">
                              {session.timestamp}
                            </span>
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </div>
                <Tooltip>
                  <TooltipTrigger render={<Button variant="ghost" size="icon" className={useProfileAccents ? "size-11 rounded-full" : undefined} onClick={() => onNewChat(persona.id)} aria-label={`Новый чат для ${persona.name}`} />}>
                    <Plus className="size-5" />
                  </TooltipTrigger>
                  <TooltipContent>Новый чат</TooltipContent>
                </Tooltip>
              </div>;
            })}
            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon"
                    className={cn("size-11 self-start rounded-full border border-dashed border-primary/60 text-primary", useProfileAccents && "ml-1")}
                    onClick={onCreatePersona}
                    aria-label="Создать профиль"
                  />
                }
              >
                <Plus className="size-6" />
              </TooltipTrigger>
              <TooltipContent>Создать профиль</TooltipContent>
            </Tooltip>
          </div>
        </SheetHeader>

        <ScrollArea className="flex-1 px-2">
          <div className="flex flex-col gap-0.5 pb-2">
            {sessions.length === 0 && (
              <p className="px-3 py-6 text-center text-sm text-muted-foreground">
                No chats yet for this persona. Start a new one.
              </p>
            )}
            {sessions.map((session) => (
              <button
                key={session.id}
                onClick={() => onSelectSession(session.id)}
                className={cn(
                  "group flex items-center gap-2 rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-white/[0.06]",
                  activeSessionId === session.id && "bg-white/[0.08]",
                )}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-foreground/90">
                    {session.title}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {session.timestamp}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                  <span
                    role="button"
                    tabIndex={-1}
                    className={cn(
                      "rounded-md p-1.5 text-muted-foreground hover:bg-white/10 hover:text-foreground",
                      session.pinned && "text-primary opacity-100",
                    )}
                  >
                    <Pin className="size-3.5" />
                  </span>
                  <span
                    role="button"
                    tabIndex={-1}
                    title="Удалить чат"
                    className="rounded-md p-1.5 text-muted-foreground hover:bg-white/10 hover:text-destructive"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteSession?.(session.id);
                    }}
                  >
                    <Trash2 className="size-3.5" />
                  </span>
                </div>
              </button>
            ))}
          </div>
        </ScrollArea>

        <div className="flex flex-col gap-1 border-t border-white/10 p-3">
          <Button
            variant="ghost"
            className="w-full justify-start gap-2 text-muted-foreground hover:text-foreground"
            onClick={onOpenSettings}
          >
            <Settings data-icon="inline-start" />
            Settings
          </Button>
        </div>
        <AlertDialog open={deletePersonaId !== null} onOpenChange={(open) => !open && setDeletePersonaId(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Удалить профиль?</AlertDialogTitle>
              <AlertDialogDescription>
                Профиль и связанные с ним настройки будут удалены. Это действие нельзя отменить.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Отмена</AlertDialogCancel>
              <AlertDialogAction
                variant="destructive"
                onClick={() => {
                  if (deletePersonaId) onDeletePersona(deletePersonaId);
                  setDeletePersonaId(null);
                }}
              >
                Удалить
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </SheetContent>
    </Sheet>
  );
}
