"""Единая защита сетевых вызовов: SSRF-валидация URL и потоковое чтение с лимитом.

Используется сетевым агентом (network_agent), инструментами поиска (search_tools)
и браузером (Playwright) ДО любого внешнего запроса к адресам, пришедшим от модели
или из поисковой выдачи.

Содержит:
- is_safe_target(url) — проверка схемы (только http/https), DNS-резолвинг всех
  IP-адресов домена и сверка с запрещёнными диапазонами (частные сети, loopback,
  link-local, резервированные);
- read_bounded(response, limit) — потоковое чтение тела с жёстким лимитом байт
  и принудительным закрытием соединения при превышении.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Лимиты загружаемого контента (защита от DoS / переполнения памяти).
MAX_HTML_BYTES = 10 * 1024 * 1024   # 10 МБ — HTML/текст
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 МБ — изображения

# Запрещённые диапазоны IPv4: частные сети, loopback, link-local, CGNAT,
# тестовые/резервированные адреса.
_BLOCKED_NETWORKS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)

_BLOCKED_IPV6 = (
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "100::/64",
    "2001:db8::/32",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)

_BLOCKED = [ipaddress.ip_network(n) for n in (*_BLOCKED_NETWORKS, *_BLOCKED_IPV6)]

# Имена хостов, всегда резолвящиеся в локальные адреса.
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _is_blocked_ip(ip) -> bool:
    return any(ip in net for net in _BLOCKED)


def is_safe_target(url) -> bool:
    """Возвращает True, если URL можно отдавать во внешний запрос.

    Разрешает только схемы http/https, резолвит ВСЕ IP-адреса домена (защита от
    DNS rebinding) и блокирует частные/loopback/link-local/резервированные адреса.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".local"):
        return False

    try:
        ips = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        ips = {ipaddress.ip_address(info[4][0]) for info in infos}

    if not ips:
        return False
    return all(not _is_blocked_ip(ip) for ip in ips)


def read_bounded(response, limit_bytes: int):
    """Потоково читает тело ответа, обрывая соединение при превышении лимита.

    Возвращает байты целиком либо None, если размер превысил limit_bytes.
    """
    chunks = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.extend(chunk)
            if len(chunks) > limit_bytes:
                return None
    finally:
        response.close()
    return bytes(chunks)
