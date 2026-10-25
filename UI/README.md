# UI — веб-интерфейс приложения (Next.js 16 + React 19 + Tailwind + shadcn/ui)

Веб-интерфейс ассистента: dev-сервер поднимается Python-мостом (`desktop_app.py` /
`WEB_UI.pyw`) командой `npm run dev -- -p 3000` и открывается в WebView-окне.
Обращается к `/api/*` на своём origin. Серверный маршрут добавляет внутренний
токен и пересылает запросы Python-мосту; браузер не получает токен или сохранённые API-ключи.

## Структура

```
UI/
├── src/
│   ├── app/                     # страницы и корневая разметка (App Router)
│   │   ├── layout.tsx           # <html>, тема, Toaster (sonner), TooltipProvider
│   │   ├── page.tsx             # AssistantPage: состояние чатов/вложений/заметок,
│   │   │                        #   все вызовы бэкенда, компоновка экрана
│   │   └── globals.css          # тема (CSS-переменные), стили чата
│   │
│   ├── components/
│   │   ├── chat/                # лента диалога
│   │   │   ├── chat-feed.tsx        # список сообщений + индикатор «думаю…»
│   │   │   ├── chat-message.tsx     # одно сообщение: markdown, вложения-скрепки,
│   │   │   │                        #   шаги рассуждений, карточка товара
│   │   │   ├── input-deck.tsx       # поле ввода: файлы (+, paste, drop), стоп, очистка
│   │   │   ├── thinking-accordion.tsx # раскрытие блока рассуждений
│   │   │   └── marketplace-card.tsx # карточка товара маркетплейса в ответе
│   │   │
│   │   ├── shell/               # каркас интерфейса
│   │   │   ├── header.tsx         # шапка: баланс, токены, статус, панели, refresh
│   │   │   ├── sidebar-drawer.tsx # боковая панель: профили и список чатов
│   │   │   └── status-pill.tsx    # пилюля статуса (ready/thinking/fetching)
│   │   │
│   │   ├── panels/              # выдвижные панели (Sheet)
│   │   │   ├── logs-panel.tsx     # логи бэкенда (/api/logs), автоскролл вниз
│   │   │   └── notes-panel.tsx    # заметки (просмотр/добавление/удаление/вложение)
│   │   │
│   │   ├── dialogs/             # модальные окна (Dialog)
│   │   │   ├── settings-modal.tsx    # бэкапы, шрифт, ключ поиска
│   │   │   ├── profiles-modal.tsx    # редактор профиля: модель, промпт, ассистент, лимиты
│   │   │   └── prompt-generator.tsx  # генератор ТЗ по идее
│   │   │
│   │   ├── widgets/             # переиспользуемые контролы
│   │   │   └── model-selector.tsx  # выбор модели из /api/models
│   │   │
│   │   └── ui/                  # примитивы shadcn/ui (button, dialog, sheet, …)
│   │                            #   — генерируются shadcn, не редактировать без нужды
│   │
│   └── lib/
│       ├── assistant-data.ts    # типы (Persona, Message, ChatSession, …) и демо-данные
│       └── utils.ts             # cn() (clsx + tailwind-merge)
│
├── public/                      # статика (картинки-плейсхолдеры)
├── package.json                 # скрипты: dev / build / start (Next.js)
├── next.config.mjs              # конфиг Next (devIndicators off, images unoptimized)
├── tsconfig.json                # TS: алиас "@/…" → "./src/…"
├── components.json              # конфиг shadcn/ui (алиасы, путь к css)
├── postcss.config.mjs           # Tailwind v4 через @tailwindcss/postcss
└── .gitignore
```

## Куда что добавлять

- Новый экран/страница → `src/app/`.
- Блок ленты чата (сообщение, вложение, карточка) → `src/components/chat/`.
- Элемент каркаса (шапка, панель чатов, индикатор) → `src/components/shell/`.
- Боковая выдвижная панель → `src/components/panels/`.
- Модальное окно настройки → `src/components/dialogs/`.
- Отдельный контрол (селектор, слайдер-блок) → `src/components/widgets/`.
- Базовый примитив из shadcn → `src/components/ui/` (через `npx shadcn@latest add …`).
- Типы данных и константы → `src/lib/assistant-data.ts`.

## Сборка и запуск

```
npm ci
npm run typecheck
npm run build    # production-сборка
```

Запускайте приложение через `../start.bat`. Для разработки перед запуском задайте
`KID_FRONTEND_DEV=1`. Python запускает Next.js с внутренним токеном текущего сеанса.
Отдельный `npm run dev` не имеет доступа к API. Установка описана в [README](../README.md).
