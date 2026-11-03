"""API provider model catalog and stale-while-revalidate balance cache."""

import json
import threading
import time
import urllib.request
import requests


class ProviderStatus:
    def __init__(self, opener, log):
        self.opener = opener
        self.log = log
        self.last_known = {"value": None}
        self.cache = {"value": None, "ts": 0.0, "key": ""}
        self.ttl = 45.0
        self.refresh_lock = threading.Lock()
        self.refresh_state = {"running": False, "ts": 0.0}
        self.refresh_min_interval = 5.0

    def balance(self, api_key, base_url="", force=False):
        if not api_key:
            return 0.0

        # Принудительное обновление (кнопка «Refresh balance» в шапке): игнорируем
        # TTL-кэш и синхронно идём в SprutDock; при сбое отдаём последнее известное.
        if force:
            balance = self.fetch(api_key)
            if balance is not None:
                return balance
            return self.last_known.get("value") or 0.0

        now = time.time()
        if (
            self.cache["key"] == api_key
            and self.cache["value"] is not None
            and now - self.cache["ts"] < self.ttl
        ):
            return self.cache["value"]

        # Просроченный кэш: отдаём последнее известное значение сразу, а обновление
        # запускаем фоном — ответ пользователю больше не ждёт сеть.
        stale = None
        if self.cache["key"] == api_key and self.cache["value"] is not None:
            stale = self.cache["value"]
        if stale is None:
            stale = self.last_known.get("value")
        if stale is not None:
            self.start_refresh(api_key)
            return stale

        # Кэша нет вовсе (первый запрос после старта) — без синхронного обращения
        # не обойтись, иначе в интерфейсе будет «0.00 ₽» до следующего сообщения.
        balance = self.fetch(api_key)
        return balance if balance is not None else 0.0


    def start_refresh(self, api_key) -> bool:
        """Запускает фоновое обновление баланса. True = обновление обеспечено.

        Защита от «стаи» потоков: не более одного одновременного запроса и не чаще
        одного раза в _BALANCE_REFRESH_MIN_INTERVAL секунд.
        """
        with self.refresh_lock:
            now = time.time()
            if self.refresh_state["running"]:
                return True
            if now - self.refresh_state["ts"] < self.refresh_min_interval:
                return False
            self.refresh_state["running"] = True
            self.refresh_state["ts"] = now

        def _worker():
            try:
                self.fetch(api_key)
            finally:
                with self.refresh_lock:
                    self.refresh_state["running"] = False

        threading.Thread(target=_worker, daemon=True).start()
        return True


    def fetch(self, api_key):
        """Синхронный GET баланса SprutDock. Возвращает float либо None при неудаче."""
        # Официальный эндпоинт SprutDock по документации: GET /v1/users/balance
        url = "https://sprutdock.ru/v1/users/balance"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        # Канал к API может отказывать по-разному: пробуем сначала как настроено
        # в системе (прокси), затем напрямую — ретраи спасают баланс от «0» при
        # сбоях TLS-рукопожатия у провайдера.
        last_error = None
        for attempt in range(2):
            try:
                if attempt == 0:
                    resp = requests.get(url, headers=headers, timeout=6)
                else:
                    resp = requests.get(
                        url, headers=headers, timeout=6, proxies={"http": None, "https": None}
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    # Ответ SprutDock содержит точный баланс в рублях
                    val = data.get("balance", data.get("data", {}).get("balance", 0.0))
                    balance = float(val)
                    self.last_known["value"] = balance
                    self.cache.update({"value": balance, "ts": time.time(), "key": api_key})
                    return balance
                last_error = f"HTTP {resp.status_code}"
            except Exception as e:
                last_error = e
        self.log(f"[!] Ошибка получения баланса SprutDock: {last_error}")
        return None


    def models(self, profile):
        api_base = profile.get("base_url") or profile.get(
            "api_base", "https://sprutdock.ru/v1"
        )
        api_key = profile.get("api_key", "")

        models_url = f"{api_base.rstrip('/')}/models"
        models_list = []
        try:
            req = urllib.request.Request(
                models_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
            )
            with self.opener.open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for m in data.get("data", []):
                    m_id = m.get("id")
                    if m_id:
                        models_list.append(
                            {
                                "id": m_id,
                                "name": m_id,
                                "owned_by": m.get("owned_by", "SprutDock"),
                            }
                        )
        except Exception as e:
            self.log(f"[!] Ошибка загрузки списка моделей ({models_url}): {e}")

        return {"models": models_list}
