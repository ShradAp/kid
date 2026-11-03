import { renderMarkdown } from "@/components/chat/message-markup";
import { ThinkingAccordion } from "@/components/chat/thinking-accordion";
import { ActionTimeline } from "@/components/chat/action-timeline";
import { MarketplaceCard } from "@/components/chat/marketplace-card";
import { Paperclip, Plus } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { Message } from "@/lib/assistant-data";
import { requestFilePreview } from "@/lib/file-actions";

function MessageAttachments({
  attachments,
}: {
  attachments: NonNullable<Message["attachments"]>;
}) {
  const visibleAttachments = attachments.slice(0, 5);
  const hiddenAttachments = attachments.slice(5);

  return (
    <span className="flex items-center gap-0.5">
      {visibleAttachments.map((attachment, index) => (
        <Tooltip key={`${attachment.name}-${index}`}>
          <TooltipTrigger
            render={
              <button
                type="button"
                onClick={() =>
                  requestFilePreview({
                    filename: attachment.name,
                    url: attachment.url,
                  })
                }
                aria-label={`Предпросмотр ${attachment.name}`}
                className="rounded p-0.5 transition-colors hover:bg-primary/15 hover:text-primary"
              />
            }
          >
            <Paperclip className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>{attachment.name}</TooltipContent>
        </Tooltip>
      ))}
      {hiddenAttachments.length > 0 && (
        <Tooltip>
          <TooltipTrigger
            render={
              <button
                type="button"
                aria-label={`Ещё файлов: ${hiddenAttachments.length}`}
                className="rounded p-0.5 transition-colors hover:bg-primary/15 hover:text-primary"
              />
            }
          >
            <Plus className="size-3.5" />
          </TooltipTrigger>
          <TooltipContent>
            <span className="flex max-w-64 flex-col gap-0.5">
              {hiddenAttachments.map((attachment, index) => (
                <span key={`${attachment.name}-${index}`}>{attachment.name}</span>
              ))}
            </span>
          </TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

export function ChatMessage({ message, streaming = false }: { message: Message; streaming?: boolean }) {
  if (message.role === "user") {
    return (
      <div className="ml-auto flex max-w-[80%] flex-col items-end gap-1 select-text">
        <div className="whitespace-pre-wrap rounded-2xl bg-primary/20 border border-primary/30 px-4 py-2.5 text-right text-[1em] leading-relaxed text-foreground select-text cursor-text">
          {message.content}
        </div>
        <div className="flex items-center gap-2 text-[0.7em] text-muted-foreground">
          <span>{message.timestamp}</span>
          {message.attachments && message.attachments.length > 0 && (
            <MessageAttachments attachments={message.attachments} />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex max-w-[85%] flex-col gap-1 select-text">
      {message.activity && !streaming && <ActionTimeline steps={message.thinking ?? []} reasoning={message.reasoning}
        durationMs={message.activity.duration_ms} interrupted={message.activity.status === "interrupted"} />}
      <div className={message.activity ? "space-y-3 select-text cursor-text" : "rounded-2xl border border-white/10 bg-card p-4 space-y-1 select-text cursor-text"}>
        {!message.activity && (message.thinking || message.reasoning) && (
          <ThinkingAccordion
            steps={message.thinking ?? []}
            reasoning={message.reasoning}
          />
        )}
        <div className="select-text cursor-text">
          {renderMarkdown(message.content)}
        </div>
        {message.images?.filter((image) => /^\/api\/codex-images\/[a-f0-9]{64}\.(png|jpg|webp|gif)$/.test(image.url)).map((image) => (
          <img key={image.url} src={image.url} alt={image.name}
            className="mt-3 block h-auto max-w-full" />
        ))}
        {message.marketplaceProduct && (
          <div className="mt-3">
            <MarketplaceCard product={message.marketplaceProduct} />
          </div>
        )}
      </div>
      <span className="ml-1 text-[0.7em] text-muted-foreground">
        {message.timestamp}
      </span>
    </div>
  );
}
