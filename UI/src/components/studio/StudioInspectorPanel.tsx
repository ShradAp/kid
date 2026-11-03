"use client";

import { useRef, useState } from "react";
import { PresetEditor } from "@/components/studio/preset-editor";
import { Download, Loader2, Paperclip, Plus, Save, Send, Trash2, Wand2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldSeparator,
  FieldTitle,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  STUDIO_RULE_PROPERTIES,
  STUDIO_RULE_SCOPES,
  type StudioPreset,
  type StudioRule,
} from "@/lib/studio-data";

const RULE_LABELS: Record<string, string> = {
  page: "Страница",
  body: "Основной текст",
  headings: "Все заголовки",
  h1: "Заголовки H1",
  h2: "Заголовки H2",
  h3: "Заголовки H3",
  captions: "Подписи",
  lists: "Списки",
  tables: "Таблицы",
  figures: "Рисунки",
  title_page: "Титульный лист",
  special_sections: "Спецразделы",
  page_numbering: "Нумерация страниц",
};


/**
 * Правая колонка: редактор оформления. Параметры наполняются из анализа
 * прикреплённого файла (или из выбранного пресета), любой числовой параметр
 * можно изменить, а также добавить/удалить собственные правила.
 */
export function StudioInspectorPanel({
  presets,
  presetId,
  onSelectPreset,
  draft,
  onChangeDraft,
  rules,
  onChangeRules,
  sourceName,
  isAnalyzing,
  onSelectSourceFile,
  onCloseDocument,
  isClosing,
  onExtractPreset,
  isExtracting,
  onReanalyze,
  onApply,
  isApplying,
  onAddPresetToChat,
  attachedPresetNames,
  onSavePreset,
  isSaving,
  onDownload,
  status,
}: {
  presets: StudioPreset[];
  presetId: string;
  onSelectPreset: (presetId: string) => void;
  draft: StudioPreset | null;
  onChangeDraft: (next: StudioPreset) => void;
  rules: StudioRule[];
  onChangeRules: (next: StudioRule[]) => void;
  sourceName: string | null;
  isAnalyzing: boolean;
  onSelectSourceFile: (file: File) => void;
  /** Закрывает документ: сбрасывает файл/пресет/анализ на бэкенде и в панели. */
  onCloseDocument: () => void;
  isClosing: boolean;
  /** Явно извлекает пресет из документа (автоматически он не создаётся). */
  onExtractPreset: () => void;
  isExtracting: boolean;
  onReanalyze: () => void;
  onApply: () => void;
  isApplying: boolean;
  onAddPresetToChat: () => void;
  attachedPresetNames: string[];
  onSavePreset: (name: string) => void;
  isSaving: boolean;
  onDownload?: () => void;
  status: string;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [presetName, setPresetName] = useState("");
  const [ruleScope, setRuleScope] = useState<string>(STUDIO_RULE_SCOPES[0]);
  const [ruleProperty, setRuleProperty] = useState<string>(STUDIO_RULE_PROPERTIES.body[0]);
  const [ruleValue, setRuleValue] = useState("");

  const presetItems: Record<string, string> = {};
  for (const item of presets) presetItems[item.id ?? item.name] = item.name;

  const propertyOptions = STUDIO_RULE_PROPERTIES[ruleScope] ?? [];

  function addRule() {
    const raw = ruleValue.trim();
    if (!raw) return;
    const numeric = Number(raw.replace(",", "."));
    const value: string | number = Number.isFinite(numeric) ? numeric : raw;
    onChangeRules([...rules, { scope: ruleScope, property: ruleProperty, value }]);
    setRuleValue("");
  }

  if (!draft) {
    return (
      <aside className="flex w-[26%] min-w-[300px] shrink-0 flex-col border-l border-white/10">
        <ScrollArea className="min-h-0 flex-1">
          <FieldGroup className="px-4 py-4">
            <Field>
              <FieldLabel>Исходный документ</FieldLabel>
              <FieldDescription>
                Прикрепите .docx — параметры оформления распознаются автоматически и
                наполнят эту панель.
              </FieldDescription>
              <input
                ref={fileInputRef}
                type="file"
                accept=".docx,.docm"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) onSelectSourceFile(file);
                  event.target.value = "";
                }}
              />
              <Button variant="outline" onClick={() => fileInputRef.current?.click()}>
                <Paperclip data-icon="inline-start" />
                Выбрать файл
              </Button>
            </Field>
          </FieldGroup>
        </ScrollArea>
      </aside>
    );
  }

  return (
    <aside className="flex w-[26%] min-w-[300px] shrink-0 flex-col border-l border-white/10">
      <ScrollArea className="min-h-0 flex-1">
        <FieldGroup className="px-4 py-4">
          <Field>
            <FieldLabel>Исходный документ</FieldLabel>
            <FieldDescription>
              Правила применяются к этому файлу; правки через чат — к нему же.
            </FieldDescription>
            <input
              ref={fileInputRef}
              type="file"
              accept=".docx,.docm"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) onSelectSourceFile(file);
                event.target.value = "";
              }}
            />
            {sourceName ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-sm text-foreground/80">
                  {sourceName}
                </span>
                <Badge variant="secondary">{status}</Badge>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="rounded-full"
                  onClick={onReanalyze}
                  aria-label="Переанализировать"
                  title="Переанализировать оформление"
                >
                  <Wand2 />
                </Button>
                {/* Явное извлечение пресета: автоматически он больше не создаётся. */}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="rounded-full"
                  disabled={isExtracting}
                  onClick={onExtractPreset}
                  aria-label="Извлечь пресет"
                  title="Извлечь пресет из документа"
                >
                  {isExtracting ? <Loader2 className="animate-spin" /> : <Plus />}
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="rounded-full"
                  disabled={isClosing}
                  onClick={onCloseDocument}
                  aria-label="Закрыть документ"
                  title="Закрыть документ и сбросить состояние студии"
                >
                  {isClosing ? <Loader2 className="animate-spin" /> : <X />}
                </Button>
              </div>
            ) : (
              <Button variant="outline" onClick={() => fileInputRef.current?.click()}>
                <Paperclip data-icon="inline-start" />
                Выбрать файл
              </Button>
            )}
          </Field>


          <FieldSeparator>Пресет оформления</FieldSeparator>

          <Field>
            <Select
              value={presetId}
              onValueChange={(value) => onSelectPreset(String(value))}
              items={presetItems}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {presets.map((item) => (
                  <SelectItem key={item.id ?? item.name} value={item.id ?? item.name}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {draft.description && <FieldDescription>{draft.description}</FieldDescription>}
          </Field>

          <Field>
            <FieldLabel>Сохранить как новый пресет</FieldLabel>
            <div className="flex items-center gap-2">
              <Input
                value={presetName}
                placeholder={draft.name}
                onChange={(event) => setPresetName(event.target.value)}
              />
              <Button
                variant="outline"
                size="icon-sm"
                disabled={isSaving}
                onClick={() => {
                  const name = presetName.trim() || draft.name;
                  onSavePreset(name);
                  setPresetName("");
                }}
                aria-label="Сохранить пресет"
              >
                {isSaving ? <Loader2 className="animate-spin" /> : <Save />}
              </Button>
            </div>
          </Field>

          <PresetEditor draft={draft} onChangeDraft={onChangeDraft} />

          <FieldSeparator>Свои правила</FieldSeparator>

          {rules.length === 0 && (
            <FieldDescription>
              Добавьте правило, чтобы точечно изменить документ: область, свойство и
              значение.
            </FieldDescription>
          )}

          {rules.map((rule, index) => (
            <Field key={`${rule.scope}_${rule.property}_${index}`} orientation="horizontal">
              <FieldContent>
                <FieldTitle>
                  {RULE_LABELS[rule.scope] ?? rule.scope} · {rule.property}
                </FieldTitle>
                <FieldDescription>{String(rule.value)}</FieldDescription>
              </FieldContent>
              <Button
                variant="ghost"
                size="icon-sm"
                className="rounded-full"
                onClick={() => onChangeRules(rules.filter((_, i) => i !== index))}
                aria-label="Удалить правило"
              >
                <Trash2 />
              </Button>
            </Field>
          ))}

          <Field>
            <FieldLabel>Новое правило</FieldLabel>
            <div className="grid grid-cols-2 gap-2">
              <Select
                value={ruleScope}
                onValueChange={(value) => {
                  const scope = String(value);
                  setRuleScope(scope);
                  setRuleProperty((STUDIO_RULE_PROPERTIES[scope] ?? [])[0] ?? "");
                }}
                items={{ ...RULE_LABELS }}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STUDIO_RULE_SCOPES.map((scope) => (
                    <SelectItem key={scope} value={scope}>
                      {RULE_LABELS[scope] ?? scope}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select
                value={ruleProperty}
                onValueChange={(value) => setRuleProperty(String(value))}
                items={Object.fromEntries(propertyOptions.map((p) => [p, p]))}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {propertyOptions.map((prop) => (
                    <SelectItem key={prop} value={prop}>
                      {prop}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center gap-2">
              <Input
                value={ruleValue}
                placeholder="значение"
                onChange={(event) => setRuleValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") addRule();
                }}
              />
              <Button variant="outline" size="icon-sm" onClick={addRule} aria-label="Добавить правило">
                <Plus />
              </Button>
            </div>
          </Field>
        </FieldGroup>
      </ScrollArea>

      <div className="flex flex-col gap-2 border-t border-white/10 p-4">
        <Button variant="outline" className="w-full" onClick={onAddPresetToChat}>
          <Send data-icon="inline-start" />
          Добавить пресет в чат
        </Button>
        {attachedPresetNames.length > 0 && (
          <FieldDescription>
            Приложено к чату: {attachedPresetNames.join(", ")}
          </FieldDescription>
        )}
        <Button className="w-full" onClick={onApply} disabled={isApplying}>
          {isApplying ? (
            <Loader2 className="animate-spin" data-icon="inline-start" />
          ) : (
            <Wand2 data-icon="inline-start" />
          )}
          Применить пресет и правила
        </Button>
        {onDownload && (
          <Button variant="outline" className="w-full" onClick={onDownload}>
            <Download data-icon="inline-start" />
            Скачать результат
          </Button>
        )}
      </div>
    </aside>
  );
}
