"use client";

import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Check, ChevronsUpDown, RefreshCw, Sparkles } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { modelCatalog, type ModelInfo } from "@/lib/assistant-data";

export function ModelSelector({
  value,
  onChange,
}: {
  value: string;
  onChange: (modelId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [search, setSearch] = useState("");
  const [models, setModels] = useState<ModelInfo[]>(modelCatalog);

  const activeModel = useMemo(
    () => models.find((m) => m.id === value),
    [models, value],
  );
  const showCustomOption =
    search.trim().length > 0 &&
    !models.some((m) => m.id.toLowerCase() === search.trim().toLowerCase());

  async function handleSync() {
    setSyncing(true);
    try {
      const res = await fetch("/api/models");
      const data = await res.json();
      if (data.models && Array.isArray(data.models) && data.models.length > 0) {
        const liveModels: ModelInfo[] = data.models.map((m: any) => ({
          id: m.id,
          label: m.name || m.id,
          provider: m.owned_by || "SprutDock",
          contextSize: "API",
          priceIn: "",
          priceOut: "",
        }));
        setModels(liveModels);
        toast.success(`Загружено ${liveModels.length} моделей`);
      } else {
        toast.error("Список моделей пуст или не получен");
      }
    } catch (e) {
      toast.error("Ошибка соединения с бэкендом");
    } finally {
      setSyncing(false);
    }
  }

  function handleSelect(modelId: string) {
    onChange(modelId);
    setOpen(false);
    setSearch("");
  }

  return (
    <div className="flex items-center gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              variant="outline"
              role="combobox"
              aria-expanded={open}
              className="min-w-0 flex-1 justify-between border-white/10 bg-white/[0.04] font-normal hover:bg-white/[0.06]"
            >
              <span className="min-w-0 truncate text-left">
                {activeModel ? activeModel.label : value || "Select a model…"}
              </span>
              <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
            </Button>
          }
        />
        <PopoverContent align="start" className="w-[340px] p-0">
          <Command shouldFilter={false}>
            <CommandInput
              placeholder="Search models…"
              value={search}
              onValueChange={setSearch}
            />
            <CommandList>
              <CommandEmpty className="py-4 text-sm text-muted-foreground">
                No models found.
              </CommandEmpty>
              <CommandGroup heading="Available models">
                {models
                  .filter((model) => {
                    const q = search.trim().toLowerCase();
                    if (!q) return true;
                    return (
                      model.id.toLowerCase().includes(q) ||
                      model.provider.toLowerCase().includes(q)
                    );
                  })
                  .map((model) => (
                    <CommandItem
                      key={model.id}
                      value={model.id}
                      onSelect={() => handleSelect(model.id)}
                      className="flex-col items-stretch gap-1 py-2"
                    >
                      <div className="flex w-full items-center justify-between gap-2">
                        <span className="truncate text-sm text-foreground">
                          {model.label}
                        </span>
                        <Check
                          className={cn(
                            "size-3.5 shrink-0 text-primary",
                            value === model.id ? "opacity-100" : "opacity-0",
                          )}
                        />
                      </div>
                      <div className="flex w-full items-center gap-1.5 text-xs text-muted-foreground">
                        <span className="rounded-full bg-white/[0.06] px-1.5 py-0.5">
                          {model.provider}
                        </span>
                        <span className="rounded-full bg-white/[0.06] px-1.5 py-0.5">
                          {model.contextSize}
                        </span>
                        <span className="truncate">
                          {model.priceIn} • {model.priceOut}
                        </span>
                      </div>
                    </CommandItem>
                  ))}
              </CommandGroup>
              {showCustomOption && (
                <CommandGroup heading="Custom">
                  <CommandItem
                    value={search}
                    onSelect={() => handleSelect(search.trim())}
                    className="gap-2"
                  >
                    <Sparkles className="size-3.5 text-muted-foreground" />
                    Use &quot;{search.trim()}&quot; as custom model name
                  </CommandItem>
                </CommandGroup>
              )}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      <Button
        variant="outline"
        size="icon"
        className="shrink-0 border-white/10 bg-white/[0.04] hover:bg-white/10"
        onClick={handleSync}
        disabled={syncing}
        aria-label="Fetch and sync model list"
      >
        <RefreshCw className={cn("size-4", syncing && "animate-spin")} />
      </Button>
    </div>
  );
}
