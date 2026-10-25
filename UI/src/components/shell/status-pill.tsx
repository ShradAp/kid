import { Brain, Globe, CircleDot } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AssistantStatus } from "@/lib/assistant-data";

const STATUS_CONFIG: Record<
  AssistantStatus,
  { label: string; icon: typeof Brain; dot: string; glow: string }
> = {
  thinking: {
    label: "Thinking...",
    icon: Brain,
    dot: "bg-primary",
    glow: "shadow-[0_0_12px_2px_theme(colors.primary/50%)]",
  },
  fetching: {
    label: "Fetching web data...",
    icon: Globe,
    dot: "bg-cyan-400",
    glow: "shadow-[0_0_12px_2px_rgba(34,211,238,0.5)]",
  },
  ready: {
    label: "Ready",
    icon: CircleDot,
    dot: "bg-emerald-400",
    glow: "shadow-[0_0_10px_2px_rgba(52,211,153,0.45)]",
  },
};

export function StatusPill({ status }: { status: AssistantStatus }) {
  const config = STATUS_CONFIG[status];

  return (
    <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 backdrop-blur-md">
      <span className="relative flex size-2">
        <span
          className={cn(
            "absolute inline-flex size-full animate-ping rounded-full opacity-60",
            config.dot,
            status !== "ready" && "animate-ping",
          )}
        />
        <span
          className={cn(
            "relative inline-flex size-2 rounded-full",
            config.dot,
            config.glow,
          )}
        />
      </span>
      <span className="text-xs font-medium text-foreground/90 text-nowrap">
        {config.label}
      </span>
    </div>
  );
}
