"""Source selection is a bounded policy, not a fixed ranking of popular sites."""
import re


CATEGORIES = {
    "primary": "Первоисточники и общий веб",
    "articles": "Статьи и разборы",
    "discussions": "Обсуждения и опыт пользователей",
    "video": "Видео",
    "social": "Публичные соцпубликации",
}


def default_scopes(query):
    text = query.casefold()
    result = []
    def add(*names):
        for name in names:
            if name not in result:
                result.append(name)
    # User-supplied platform clues have priority over inferred topic preferences.
    for pattern, scope in ((r"тик.?ток|tiktok", "tiktok"), (r"youtube|ютуб", "youtube"),
                           (r"телеграм|telegram|\bтг\b", "telegram"), (r"\bвк\b|vk\.\w+|вконтакте", "vk"),
                           (r"пикабу|pikabu", "pikabu"), (r"github|hugging.?face", "technical")):
        if re.search(pattern, text):
            add(scope)
    if re.search(r"нейросет|\bai\b|\bаи\b|llm|подписк|тариф|приложени|subscription|pricing", text):
        add("apps", "telegram", "youtube", "forums")
    elif re.search(r"ошибк|баг|python|javascript|библиотек|\bapi\b|код\b", text):
        add("technical", "forums", "articles")
    elif re.search(r"отзыв|сравн|купить|покуп|опыт использ|review|comparison|buy\b", text):
        add("reviews", "forums", "youtube", "articles")
    elif re.search(r"новост|произош|событи|сегодня|\bnews\b|\btoday\b", text):
        add("articles", "telegram")
    elif not result:
        add("articles", "forums")
    return result[:4]


def validate_scopes(value, catalog, limit=4):
    if not isinstance(value, list):
        return None
    result = []
    for name in value:
        if isinstance(name, str) and name in catalog and name not in result:
            result.append(name)
        if len(result) >= limit:
            break
    return result if value == [] or result else None
