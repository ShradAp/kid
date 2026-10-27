"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Field,
  FieldLabel,
  FieldGroup,
  FieldDescription,
  FieldSeparator,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { SecretInput } from "@/components/ui/secret-input";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ModelSelector } from "@/components/widgets/model-selector";
import type { Persona } from "@/lib/assistant-data";
import { CodexSettings, defaultCodexSettings } from "./codex-settings";
import { profileColors } from "@/lib/profile-colors";

export function ProfilesModal({
  open,
  onOpenChange,
  persona,
  onSave,
  onDelete,
  canDelete = true,
  allowCodex = true,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  persona: Persona;
  onSave: (persona: Persona) => void | Promise<void>;
  onDelete?: (id: string) => Promise<boolean>;
  canDelete?: boolean;
  allowCodex?: boolean;
}) {
  const [name, setName] = useState(persona.name ?? "");
  const [color, setColor] = useState(persona.color ?? "auto");
  const [engine, setEngine] = useState(persona.engine ?? "api");
  const [codex, setCodex] = useState(persona.codex ?? { ...defaultCodexSettings });
  const [systemPrompt, setSystemPrompt] = useState(persona.systemPrompt ?? "");
  const [temperature, setTemperature] = useState(persona.temperature ?? 0.7);
  const [baseUrl, setBaseUrl] = useState(persona.baseUrl ?? "");
  const [apiKey, setApiKey] = useState(persona.apiKey ?? "");
  const [clearApiKey, setClearApiKey] = useState(false);
  const [modelName, setModelName] = useState(persona.modelName ?? "");
  const [maxSteps, setMaxSteps] = useState(persona.maxSteps ?? 60);
  const [attachMaxImages, setAttachMaxImages] = useState(persona.attachMaxImages ?? 4);
  const [attachMaxTextChars, setAttachMaxTextChars] = useState(persona.attachMaxTextChars ?? 60000);
  const [attachMaxTextPerFile, setAttachMaxTextPerFile] = useState(persona.attachMaxTextPerFile ?? 60000);
  const [attachMaxTotalText, setAttachMaxTotalText] = useState(persona.attachMaxTotalText ?? 180000);
  const [attachCsvMaxRows, setAttachCsvMaxRows] = useState(persona.attachCsvMaxRows ?? 400);
  const [attachXlsxMaxRows, setAttachXlsxMaxRows] = useState(persona.attachXlsxMaxRows ?? 400);
  const [chatMsgMaxChars, setChatMsgMaxChars] = useState(persona.chatMsgMaxChars ?? 0);
  const [chatMsgTextPartMaxChars, setChatMsgTextPartMaxChars] = useState(persona.chatMsgTextPartMaxChars ?? 0);
  const [attachLimitsOpen, setAttachLimitsOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (open) {
      setEngine(persona.engine ?? "api");
      setCodex({ ...(persona.codex ?? defaultCodexSettings), proxy_password: "", clear_proxy_password: false });
      setName(persona.name ?? "");
      setColor(persona.color ?? "auto");
      setSystemPrompt(persona.systemPrompt ?? "");
      setTemperature(persona.temperature ?? 0.7);
      setBaseUrl(persona.baseUrl ?? "");
      setApiKey(persona.apiKey ?? "");
      setClearApiKey(false);
      setModelName(persona.modelName ?? "");
      setMaxSteps(persona.maxSteps ?? 60);
      setAttachMaxImages(persona.attachMaxImages ?? 4);
      setAttachMaxTextChars(persona.attachMaxTextChars ?? 60000);
      setAttachMaxTextPerFile(persona.attachMaxTextPerFile ?? 60000);
      setAttachMaxTotalText(persona.attachMaxTotalText ?? 180000);
      setAttachCsvMaxRows(persona.attachCsvMaxRows ?? 400);
      setAttachXlsxMaxRows(persona.attachXlsxMaxRows ?? 400);
      setChatMsgMaxChars(persona.chatMsgMaxChars ?? 0);
      setChatMsgTextPartMaxChars(persona.chatMsgTextPartMaxChars ?? 0);
      setAttachLimitsOpen(false);
    }
  }, [open, persona]);

  async function handleSave() {
    setSaving(true);
    try {
      await onSave({
        ...persona,
        color,
        engine,
        codex,
        name,
        systemPrompt,
        maxSteps: Number.isFinite(maxSteps) && maxSteps >= 1 && maxSteps <= 200 ? maxSteps : 25,
        attachMaxImages: Math.max(1, attachMaxImages),
        attachMaxTextChars: Math.max(100, attachMaxTextChars),
        attachMaxTextPerFile: Math.max(100, attachMaxTextPerFile),
        attachMaxTotalText: Math.max(100, attachMaxTotalText),
        attachCsvMaxRows: Math.max(1, attachCsvMaxRows),
        attachXlsxMaxRows: Math.max(1, attachXlsxMaxRows),
        chatMsgMaxChars: Math.max(0, chatMsgMaxChars),
        chatMsgTextPartMaxChars: Math.max(0, chatMsgTextPartMaxChars),
        temperature,
        baseUrl,
        apiKey,
        clearApiKey,
        hasApiKey: !clearApiKey && Boolean(apiKey || persona.hasApiKey),
        modelName,
      });
      toast.success("Profile updated");
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Не удалось сохранить профиль");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-white/10 bg-popover sm:max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Profile & system prompt manager</DialogTitle>
          <DialogDescription>
            Edit this persona&apos;s identity, model connection, and behavior.
          </DialogDescription>
        </DialogHeader>

        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="persona-name">Persona name</FieldLabel>
            <Input
              id="persona-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          {allowCodex && <Field>
            <FieldLabel>Цвет профиля</FieldLabel>
            <div className="flex flex-wrap gap-2" role="group" aria-label="Цвет профиля">
              <Button type="button" variant="outline" size="sm" aria-pressed={color === "auto"} onClick={() => setColor("auto")}>Авто</Button>
              {profileColors.map((item) => <Button key={item.id} type="button" variant="ghost" size="icon" aria-label={item.name} title={item.name}
                aria-pressed={color === item.id} onClick={() => setColor(item.id)}
                className="size-8 rounded-full border-2"
                style={{ backgroundColor: `${item.hex}40`, borderColor: color === item.id ? item.hex : "transparent" }}>
                <span className="size-3 rounded-full" style={{ backgroundColor:item.hex }} />
              </Button>)}
            </div>
          </Field>}

           <Field>
            <FieldLabel htmlFor="system-prompt">System prompt</FieldLabel>
            <Textarea
              id="system-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              className="h-36 max-h-48 overflow-y-auto resize-y font-mono text-xs leading-relaxed"
              placeholder="Enter system prompt instructions..."
            />
          </Field>

          <FieldSeparator />

          {allowCodex && <Field>
            <FieldLabel htmlFor="profile-engine">Движок</FieldLabel>
            <Select value={engine} onValueChange={(value) => { if (value) setEngine(value); }}>
              <SelectTrigger id="profile-engine" className="w-full"><SelectValue>{engine === "codex" ? "Codex — изолированный App Server" : "API — OpenAI-совместимый"}</SelectValue></SelectTrigger>
              <SelectContent>
                <SelectItem value="api">API — OpenAI-совместимый</SelectItem>
                <SelectItem value="codex">Codex — изолированный App Server</SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription>Настройки обоих режимов сохраняются отдельно.</FieldDescription>
          </Field>}
          {engine === "codex" && <CodexSettings profileId={persona.id} value={codex} onChange={setCodex} />}
          {engine === "api" && <>

          <Field>
            <FieldLabel htmlFor="base-url">Base URL</FieldLabel>
            <Input
              id="base-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://generativelanguage.googleapis.com/v1beta"
            />
            <FieldDescription>
              Endpoint for this persona&apos;s provider. Supports
              OpenAI-compatible APIs.
            </FieldDescription>
          </Field>

          <Field>
            <FieldLabel htmlFor="api-key">API key</FieldLabel>
            <SecretInput key={persona.id} id="api-key" label="API key" value={apiKey} cleared={clearApiKey}
              saved={persona.hasApiKey && !clearApiKey} reveal={allowCodex ? {field:"profile_api_key",profile_id:persona.id} : undefined}
              onChange={(key) => { setApiKey(key); setClearApiKey(false); }} />
            <Button type="button" variant="ghost" size="sm" onClick={() => { setApiKey(""); setClearApiKey(true); }}>
              Удалить сохранённый ключ при сохранении
            </Button>
          </Field>

          <Field>
            <FieldLabel htmlFor="model-name">Model</FieldLabel>
            <ModelSelector value={modelName} onChange={setModelName} />
            <FieldDescription>
              Search by name or provider, or type a custom model identifier.
            </FieldDescription>
          </Field>

           <FieldSeparator />
          </>}

          <Field>
            <FieldLabel htmlFor="max-steps">Максимум шагов инструментов</FieldLabel>
            <Input id="max-steps" type="number" min={1} max={200} value={maxSteps} onChange={(e) => setMaxSteps(Math.max(1, Math.min(200, Number(e.target.value) || 60)))} />
          </Field>

          <div className="rounded-lg border border-white/10 bg-white/[0.02] overflow-hidden">
            <button type="button" className="flex w-full items-center justify-between p-3 text-left text-sm font-medium" onClick={() => setAttachLimitsOpen((v) => !v)}>
              <span>Лимиты вложений (файлы)</span>
              {attachLimitsOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            </button>
            {attachLimitsOpen && (
              <div className="space-y-3 border-t border-white/10 p-3">
                {[
                  ["attach-max-images", "Картинок за раз", attachMaxImages, setAttachMaxImages, "Максимум изображений в одном запросе"],
                  ["attach-max-text-chars", "Текст документа, символов", attachMaxTextChars, setAttachMaxTextChars, "Сколько текста читается из одного файла"],
                  ["attach-max-text-per-file", "Файл в промпт, символов", attachMaxTextPerFile, setAttachMaxTextPerFile, "Сколько символов одного файла уходит в модель"],
                  ["attach-max-total-text", "Всего текста, символов", attachMaxTotalText, setAttachMaxTotalText, "Общий потолок на все текстовые вложения"],
                  ["attach-csv-max-rows", "Строк в CSV/TSV", attachCsvMaxRows, setAttachCsvMaxRows, "Сколько строк таблицы CSV/TSV читается из файла"],
                  ["attach-xlsx-max-rows", "Строк в XLSX (на лист)", attachXlsxMaxRows, setAttachXlsxMaxRows, "Сколько строк читается с каждого листа XLSX"],
                ].map(([id, label, value, setter, description]) => (
                  <Field key={id as string}>
                    <FieldLabel htmlFor={id as string}>{label as string}</FieldLabel>
                    <Input id={id as string} type="number" min={1} value={value as number} onChange={(e) => (setter as (value: number) => void)(Math.max(1, Number(e.target.value) || 1))} />
                    <FieldDescription>{description as string}</FieldDescription>
                  </Field>
                ))}
                <Field>
                  <FieldLabel htmlFor="chat-msg-max-chars">Сообщение в модель, символов (0 = без лимита)</FieldLabel>
                  <Input id="chat-msg-max-chars" type="number" min={0} value={chatMsgMaxChars} onChange={(e) => setChatMsgMaxChars(Math.max(0, Number(e.target.value) || 0))} />
                  <FieldDescription>Потолок на одно текстовое сообщение перед отправкой в API. 0 отключает обрезку — вложения и запрос доходят целиком (ограничены только «Всего текста»).</FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="chat-msg-text-part-max-chars">Текстовая часть с картинками, символов (0 = без лимита)</FieldLabel>
                  <Input id="chat-msg-text-part-max-chars" type="number" min={0} value={chatMsgTextPartMaxChars} onChange={(e) => setChatMsgTextPartMaxChars(Math.max(0, Number(e.target.value) || 0))} />
                  <FieldDescription>Потолок на текстовую часть сообщения, когда в запросе есть изображения. 0 отключает обрезку.</FieldDescription>
                </Field>
              </div>
            )}
          </div>

          {engine === "api" && <Field>
            <FieldLabel>
              Model temperature — {temperature.toFixed(1)}
            </FieldLabel>
            <Slider
              min={0}
              max={1}
              step={0.1}
              value={temperature}
              onValueChange={(v) => setTemperature(v as number)}
              className="py-2"
            />
            <FieldDescription>
              Lower is more focused and deterministic, higher is more creative.
            </FieldDescription>
          </Field>}
        </FieldGroup>

        <DialogFooter className="sticky bottom-0 bg-popover/90 backdrop-blur pt-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={saving} onClick={() => void handleSave()}>Save changes</Button>
        </DialogFooter>
        {onDelete && <div className="border-t border-white/10 pt-4">
          <Button type="button" variant="destructive" className="w-full" disabled={!canDelete || saving || deleting} onClick={() => setDeleteOpen(true)}>
            <Trash2 className="size-4" />Удалить профиль
          </Button>
          {!canDelete && <p className="mt-2 text-xs text-muted-foreground">Нельзя удалить единственный профиль.</p>}
        </div>}
        <AlertDialog open={deleteOpen} onOpenChange={(next) => { if (!deleting) setDeleteOpen(next); }}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Удалить профиль «{persona.name}»?</AlertDialogTitle>
              <AlertDialogDescription>Профиль и связанные с ним настройки будут удалены. Это действие нельзя отменить.</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={deleting}>Отмена</AlertDialogCancel>
              <AlertDialogAction variant="destructive" disabled={deleting} onClick={async (event) => {
                event.preventDefault();
                if (!onDelete || deleting) return;
                setDeleting(true);
                try {
                  if (await onDelete(persona.id)) { setDeleteOpen(false); onOpenChange(false); }
                } catch { toast.error("Не удалось удалить профиль"); }
                finally { setDeleting(false); }
              }}>{deleting ? "Удаление…" : "Удалить"}</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </DialogContent>
    </Dialog>
  );
}
