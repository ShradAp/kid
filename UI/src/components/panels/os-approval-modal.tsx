"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

export interface OsPendingAction {
  id: string;
  operation: string;
  command: string;
  description: string;
  consequences: string;
  status: string;
}

const BRIDGE = "";

// Самостоятельная модалка: сама опрашивает очередь ожидающих подтверждения
// Windows-операций. Пока очередь не пуста — показывает точную команду, назначение
// и последствия; выполнение идёт только после явного «Подтвердить».
export function OsApprovalModal() {
  const [actions, setActions] = useState<OsPendingAction[]>([]);
  const [deciding, setDeciding] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let failures = 0;
    const poll = async () => {
      try {
        const res = await fetch(`${BRIDGE}/api/os-tools/pending`);
        if (!res.ok) throw new Error("Backend unavailable");
        const data = await res.json();
        if (!cancelled && Array.isArray(data.pending)) {
          failures = 0;
          setActions(data.pending);
        }
      } catch {
        failures += 1;
        if (!cancelled && failures >= 2) setActions([]);
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  async function decide(id: string, approve: boolean) {
    if (deciding !== null) return;
    setDeciding(id);
    try {
      const res = await fetch(
        `${BRIDGE}/api/os-tools/${approve ? "approve" : "reject"}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        },
      );
      const data = (await res.json().catch(() => ({}))) as { ok?: boolean };
      if (!res.ok || !data.ok) {
        toast.error("Не удалось применить решение");
        return;
      }
      setActions((prev) => prev.filter((a) => a.id !== id));
      toast.success(approve ? "Операция подтверждена" : "Операция отклонена");
    } catch {
      toast.error("Ошибка соединения с бэкендом");
    } finally {
      setDeciding(null);
    }
  }

  if (actions.length === 0) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-2xl rounded-2xl border border-amber-500/40 bg-[#121218] p-5 shadow-2xl">
        <div className="mb-3 flex items-center gap-2">
          <span className="text-lg">🖥️</span>
          <h2 className="text-base font-semibold text-foreground">
            Требуется подтверждение операции
          </h2>
          <span className="ml-auto rounded bg-amber-500/15 px-2 py-0.5 text-xs font-semibold text-amber-400">
            {actions.length}
          </span>
        </div>
        <p className="mb-4 text-sm text-muted-foreground">
          Подтвердите конкретное действие модели. Для Codex разрешение действует
          действует только для показанного действия. Инструменты Windows работают на вашем ПК; сетевую изоляцию подтверждение не снимает.
        </p>

        <div className="flex max-h-[60vh] flex-col gap-3 overflow-y-auto">
          {actions.map((action) => (
            <div
              key={action.id}
              className="rounded-xl border border-white/10 bg-white/[0.03] p-4"
            >
              <div className="mb-1 text-sm font-semibold text-foreground">
                {action.operation}
              </div>
              <div className="mb-2 text-sm text-foreground/90">
                {action.description}
              </div>
              {action.consequences && (
                <div className="mb-2 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
                  ⚠️ {action.consequences}
                </div>
              )}
              <div className="mb-3 break-all rounded-lg bg-black/40 p-3 font-mono text-xs leading-relaxed text-emerald-300">
                {action.command}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={deciding !== null}
                  onClick={() => void decide(action.id, true)}
                  className="rounded-lg bg-emerald-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-emerald-500"
                >
                  Подтвердить
                </button>
                <button
                  type="button"
                  disabled={deciding !== null}
                  onClick={() => void decide(action.id, false)}
                  className="rounded-lg bg-destructive/80 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-destructive"
                >
                  Отклонить
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
