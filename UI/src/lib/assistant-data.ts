export type AssistantStatus = "ready" | "thinking" | "fetching";

export interface Persona {
  color?: string;
  engine?: "api" | "codex";
  codex?: CodexSettings;
  maxSteps?: number
  attachMaxImages?: number
  attachMaxTextChars?: number
  attachMaxTextPerFile?: number
  attachMaxTotalText?: number
  attachCsvMaxRows?: number
  attachXlsxMaxRows?: number
  chatMsgMaxChars?: number
  chatMsgTextPartMaxChars?: number
  id: string;
  emoji: string;
  name: string;
  description: string;
  systemPrompt: string;
  temperature: number;
  baseUrl: string;
  apiKey: string;
  hasApiKey?: boolean;
  clearApiKey?: boolean;
  modelName: string;
}

export interface CodexSettings {
  auto_approve_commands?: boolean;
  model: string;
  effort: string;
  search: "codex" | "kid";
  proxy: string;
  proxy_password?: string;
  has_proxy_password?: boolean;
  clear_proxy_password?: boolean;
  workspace: string;
}

export interface ChatSession {
  id: string;
  personaId: string;
  title: string;
  timestamp: string;
  pinned?: boolean;
}

export interface ModelInfo {
  id: string;
  label: string;
  provider: string;
  contextSize: string;
  priceIn: string;
  priceOut: string;
}

export interface MarketplaceProduct {
  image: string;
  title: string;
  price: string;
  rating: number;
  badges: string[];
}

export interface Message {
  activity?: { duration_ms?: number; status?: string };
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  attachments?: Array<{
    name: string;
    url: string;
  }>;
  thinking?: string[];
  reasoning?: string;
  marketplaceProduct?: MarketplaceProduct;
}

// Профили и чаты приходят ТОЛЬКО с бэкенда (/api/init).
// Демо-персоны и фиктивные чаты удалены: интерфейс не должен показывать
// несуществующие профили (они «съедали» реальные настройки и создавали
// чаты, привязанные к демо-id вроде "master").
export const personas: Persona[] = [];

export const modelCatalog: ModelInfo[] = [
  {
    id: "gemini-2.5-flash",
    label: "gemini-2.5-flash",
    provider: "Google",
    contextSize: "1M ctx",
    priceIn: "0.15 ₽ / 1k in",
    priceOut: "0.45 ₽ / 1k out",
  },
  {
    id: "gemini-2.5-pro",
    label: "gemini-2.5-pro",
    provider: "Google",
    contextSize: "2M ctx",
    priceIn: "1.05 ₽ / 1k in",
    priceOut: "3.20 ₽ / 1k out",
  },
  {
    id: "gpt-4.1",
    label: "gpt-4.1",
    provider: "OpenAI",
    contextSize: "128k ctx",
    priceIn: "1.80 ₽ / 1k in",
    priceOut: "5.40 ₽ / 1k out",
  },
  {
    id: "gpt-4o",
    label: "gpt-4o",
    provider: "OpenAI",
    contextSize: "128k ctx",
    priceIn: "2.30 ₽ / 1k in",
    priceOut: "6.90 ₽ / 1k out",
  },
  {
    id: "claude-3-7-sonnet",
    label: "claude-3-7-sonnet",
    provider: "Anthropic",
    contextSize: "200k ctx",
    priceIn: "2.70 ₽ / 1k in",
    priceOut: "10.80 ₽ / 1k out",
  },
  {
    id: "claude-3-5-haiku",
    label: "claude-3-5-haiku",
    provider: "Anthropic",
    contextSize: "200k ctx",
    priceIn: "0.72 ₽ / 1k in",
    priceOut: "3.60 ₽ / 1k out",
  },
  {
    id: "deepseek-v3",
    label: "deepseek-v3",
    provider: "DeepSeek",
    contextSize: "64k ctx",
    priceIn: "0.10 ₽ / 1k in",
    priceOut: "0.30 ₽ / 1k out",
  },
];

export const sessionMessages: Record<string, Message[]> = {};
