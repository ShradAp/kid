"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ChevronDown, ChevronRight, Eye, EyeOff } from "lucide-react";
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
import {
  InputGroup,
  InputGroupInput,
  InputGroupAddon,
  InputGroupButton,
} from "@/components/ui/input-group";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { Button } from "@/components/ui/button";
import { ModelSelector } from "@/components/widgets/model-selector";
import type { Persona } from "@/lib/assistant-data";

export function ProfilesModal({
  open,
  onOpenChange,
  persona,
  onSave,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  persona: Persona;
  onSave: (persona: Persona) => void;
}) {
  const [name, setName] = useState(persona.name ?? "");
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
  const [showApiKey, setShowApiKey] = useState(false);

  useEffect(() => {
    if (open) {
      setName(persona.name ?? "");
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
      setShowApiKey(false);
    }
  }, [open, persona]);

  function handleSave() {
    onSave({
      ...persona,
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
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-white/10 bg-popover sm:max-w-lg max-h-[85vh] overflow-y-auto pr-3">
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
            <InputGroup>
              <InputGroupInput
                id="api-key"
                type={showApiKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => { setApiKey(e.target.value); setClearApiKey(false); }}
                placeholder={persona.hasApiKey && !clearApiKey ? "Ключ сохранён. Введите новый для замены" : "API key"}
              />
              <InputGroupAddon align="inline-end">
                <InputGroupButton
                  size="icon-xs"
                  aria-label={showApiKey ? "Hide API key" : "Show API key"}
                  onClick={() => setShowApiKey((v) => !v)}
                >
                  {showApiKey ? <EyeOff /> : <Eye />}
                </InputGroupButton>
              </InputGroupAddon>
            </InputGroup>
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

          <Field>
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
          </Field>
        </FieldGroup>

        <DialogFooter className="sticky bottom-0 bg-popover/90 backdrop-blur pt-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save changes</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
