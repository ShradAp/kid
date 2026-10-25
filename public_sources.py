"""Guest-only discovery and reading of public social/video material.

No browser profiles, saved cookies or credentials are used. Page formats are
best-effort adapters: unsupported/blocked content is reported explicitly.
"""
import html
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import xml.etree.ElementTree as ET

import requests

from guest_transport import GuestSession, GuestTransportError
from youtube_captions import fetch_captions
from tiktok_media import read_tiktok_media
from vk_public import read_vk_fallback


DOMAINS = {
    "youtube": ("youtube.com", "youtu.be"),
    "vk": ("vk.com", "vk.ru"),
    "tiktok": ("tiktok.com",),
    "telegram": ("t.me", "telegram.me"),
}
LABELS = {"youtube": "YouTube", "vk": "ВК", "tiktok": "TikTok", "telegram": "Telegram"}
SEARCH_DOMAINS = {**DOMAINS,
    "forums": ("4pda.to", "reddit.com"),
    "reviews": ("otzovik.com", "irecommend.ru"),
    "articles": ("habr.com", "vc.ru"),
    "pikabu": ("pikabu.ru",),
    "technical": ("github.com", "huggingface.co"),
    "apps": ("play.google.com", "apps.apple.com", "rustore.ru"),
}
LABELS.update(forums="Форумы и обсуждения", reviews="Отзывы", articles="Статьи и сообщества", technical="GitHub и Hugging Face")
LABELS["apps"] = "Магазины приложений"
LABELS["pikabu"] = "Пикабу (дополнительное направление)"


def scope_for(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return next((name for name, domains in SEARCH_DOMAINS.items()
                 if any(host == d or host.endswith("."+d) for d in domains)), "")


def platform_for(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return next((name for name, domains in DOMAINS.items()
                 if any(host == d or host.endswith("." + d) for d in domains)), "")


def scoped_query(query, platform):
    domains = SEARCH_DOMAINS[platform]
    scope = " OR ".join("site:" + d for d in domains)
    return f"({scope}) {query}" if len(domains) > 1 else f"{scope} {query}"


def useful_candidate(url):
    platform = platform_for(url)
    parts = urlsplit(url)
    if platform == "tiktok":
        return bool(re.search(r"/video/\d+", parts.path))
    if platform == "youtube":
        return bool(re.fullmatch(r"/[\w-]{11}/?", parts.path) if parts.hostname == "youtu.be" else
                    re.fullmatch(r"[\w-]{11}", parse_qs(parts.query).get("v", [""])[0]) or
                    re.fullmatch(r"/(?:shorts|embed)/[\w-]{11}/?", parts.path))
    return True


def public_get(url, timeout=10, *, transport=None):
    """Bound response size and redirects; revalidate each redirect target."""
    if transport is None:
        with GuestSession(timeout) as session:
            return public_get(url, timeout, transport=session)
    response = transport.get(url)
    response.raise_for_status()
    return response.text


def embedded_json(text, marker):
    """Decode a single JSON value, rather than matching nested braces by regex."""
    for match in re.finditer(re.escape(marker), text):
        start = text.find("{", match.end(), match.end() + 300)
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except ValueError:
                pass
    return {}


def walk(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def _text(value):
    return value.get("simpleText") or "".join(str(r.get("text", "")) for r in value.get("runs", []))


def youtube_search(query, page=1, count=10, timeout=8):
    if page != 1:
        return {"items": [], "status": "unsupported", "error": "guest_continuation_unavailable", "has_more": False}
    try:
        page_text = public_get("https://www.youtube.com/results?" + urlencode({"search_query": query}), timeout)
        data = embedded_json(page_text, "ytInitialData")
        items, seen = [], set()
        for node in walk(data):
            video = node.get("videoRenderer") or node.get("gridVideoRenderer")
            if not video:
                continue
            video_id = video.get("videoId", "")
            if not re.fullmatch(r"[\w-]{11}", video_id) or video_id in seen:
                continue
            seen.add(video_id)
            items.append({"url": "https://www.youtube.com/watch?v=" + video_id,
                          "title": _text(video.get("title", {})),
                          "body": _text(video.get("descriptionSnippet", {})),
                          "author": _text(video.get("ownerText", {})), "platform": "youtube"})
        if not data:
            return {"items": [], "status": "unavailable", "error": "guest_search_blocked_or_format_changed", "has_more": False}
        return {"items": items[:count], "status": "ok" if items else "empty", "has_more": False}
    except Exception as exc:
        return {"items": [], "status": "unavailable", "error": type(exc).__name__, "has_more": False}


class PublicHTML(HTMLParser):
    """Read explicit post containers and metadata, excluding navigation/login UI."""
    def __init__(self, classes=(), scope_ids=()):
        super().__init__(convert_charrefs=True)
        self.classes = set(classes)
        self.stack, self.parts, self.meta = [], [], {}
        self.active = 0
        self.scope_ids, self.scoped = set(scope_ids), 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property") or attrs.get("name")] = attrs.get("content", "")
        if tag in {"br", "hr"}:
            if self.active:
                self.parts.append("\n")
            return
        if tag in {"meta", "img", "input", "link", "source", "wbr", "area", "embed", "param"}:
            return
        scope = attrs.get("id") in self.scope_ids or attrs.get("data-post-id") in self.scope_ids
        matches = bool(self.classes.intersection(attrs.get("class", "").split())) and (
            not self.scope_ids or self.scoped or scope)
        blocked = tag in {"script", "style", "noscript"} or (self.stack and self.stack[-1][2])
        self.stack.append((tag, matches, bool(blocked), scope))
        self.active += matches
        self.scoped += scope

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                self.active -= sum(s[1] for s in self.stack[i:])
                self.scoped -= sum(s[3] for s in self.stack[i:])
                del self.stack[i:]
                if tag in {"div", "p", "blockquote"}:
                    self.parts.append("\n")
                break

    def handle_data(self, data):
        if self.active and not (self.stack and self.stack[-1][2]):
            self.parts.append(data)

    @property
    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def read_public(url, timeout=18, *, fetch_transcript=True, allow_audio=False, allow_browser=True, query=""):
    platform = platform_for(url)
    if not platform:
        return None
    with GuestSession(timeout, allowed_domains=DOMAINS[platform]) as transport:
        result = _read_public(url, timeout, transport, fetch_transcript, allow_audio, allow_browser, query)
        result["transport"] = transport.diagnostics()
        return result


def _read_public(url, timeout, transport, fetch_transcript, allow_audio=False, allow_browser=True, query=""):
    """Return None for ordinary web URLs; never fall back to an authenticated browser."""
    platform = platform_for(url)
    if not platform:
        return None
    result = {"url": url, "platform": platform, "access": "guest", "text_content": ""}
    deadline = time.monotonic() + timeout
    def fetch(target, cap=12):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("public_read_timeout")
        return public_get(target, min(cap, remaining), transport=transport)
    try:
        if platform == "telegram":
            # Widgets address exactly one public message, avoiding channel-feed contamination.
            path = urlsplit(url).path.strip("/").split("/")
            if path and path[0] == "s":
                path = path[1:]
            if len(path) != 2 or not re.fullmatch(r"[A-Za-z0-9_]+", path[0]) or not path[1].isdigit():
                return {**result, "error": "public_message_url_required", "content_kind": "unavailable"}
            text = fetch(f"https://t.me/{path[0]}/{path[1]}?embed=1")
            parser = PublicHTML(("tgme_widget_message_text", "tgme_widget_message_caption"))
            parser.feed(text)
            if parser.text:
                return {**result, "text_content": parser.text, "content_kind": "post"}
        elif platform == "youtube":
            parts = urlsplit(url)
            video_id = parts.path.strip("/") if parts.hostname == "youtu.be" else parse_qs(parts.query).get("v", [""])[0]
            if not video_id:
                match = re.match(r"/(?:shorts|embed)/([\w-]{11})", parts.path)
                video_id = match[1] if match else ""
            if not re.fullmatch(r"[\w-]{11}", video_id):
                return {**result, "error": "video_url_required", "content_kind": "unavailable"}
            caption_status = "not_exposed" if fetch_transcript else "search_budget_skipped"
            if fetch_transcript:
                caption = fetch_captions(video_id, transport)
                caption_status = caption.get("caption_status", "unavailable")
                if caption.get("content_kind") == "transcript":
                    return {**result, **caption}
            text = fetch("https://www.youtube.com/watch?v=" + video_id)
            player = embedded_json(text, "ytInitialPlayerResponse")
            details = player.get("videoDetails", {})
            tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
            if tracks and fetch_transcript:
                track = next((t for t in tracks if t.get("languageCode") == "ru"), tracks[0])
                caption_url = track.get("baseUrl", "")
                if platform_for(caption_url) == "youtube":
                    try:
                        xml = fetch(caption_url, 5)
                        root = ET.fromstring(xml)
                        lines, count, truncated = [], 0, False
                        for node in root.iter():
                            if node.tag not in ("text", "p"):
                                continue
                            seconds = float(node.get("start", "0")) if node.tag == "text" else float(node.get("t", "0"))/1000
                            seconds = max(0, int(seconds))
                            stamp = f"{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}"
                            line = f"[{stamp}] {html.unescape(''.join(node.itertext()))}"
                            if len(lines) >= 2500 or count+len(line)+1 > 60000:
                                truncated = True
                                break
                            lines.append(line)
                            count += len(line)+1
                        if lines:
                            return {**result, "title": details.get("title", ""), "author": details.get("author", ""),
                                    "text_content": "\n".join(lines), "content_kind": "transcript",
                                    "transcript_truncated": truncated, "caption_provider": "youtube-player-xml",
                                    "caption_language": track.get("languageCode"), "automatic_captions": track.get("kind") == "asr"}
                        caption_status = "empty"
                    except Exception:
                        caption_status = "unavailable"
            if details:
                return {**result, "title": details.get("title", ""), "author": details.get("author", ""),
                        "text_content": details.get("shortDescription") or details.get("title", ""),
                        "content_kind": "video_description", "caption_status": caption_status}
        elif platform == "tiktok":
            video_match = re.search(r"/video/(\d+)", url)
            if not video_match:
                return {**result, "error": "video_url_required", "content_kind": "unavailable"}
            text = ""
            try:
                text = fetch(url)
            except (requests.RequestException, ValueError):
                pass
            state = embedded_json(text, '__UNIVERSAL_DATA_FOR_REHYDRATION__') or embedded_json(text, "SIGI_STATE")
            media = {} if fetch_transcript else {"media_status": "search_budget_skipped"}
            result.update(media)
            if fetch_transcript and deadline - time.monotonic() > 1:
                media = read_tiktok_media(url, timeout=max(.1, deadline-time.monotonic()), allow_audio=allow_audio, query=query)
                if media.get("text_content"):
                    return {**result, **media}
                result.update(media)
            for node in walk(state):
                if isinstance(node.get("desc"), str) and node.get("id"):
                    if str(node["id"]) != video_match[1]:
                        continue
                    return {**result, "text_content": node["desc"], "content_kind": "video_description"}
            try:
                embed = json.loads(fetch("https://www.tiktok.com/oembed?" + urlencode({"url": url})))
                if embed.get("title"):
                    return {**result, "text_content": str(embed["title"]), "author": embed.get("author_name", ""),
                            "content_kind": "video_description"}
            except (requests.RequestException, ValueError, TimeoutError):
                pass
        else:
            try:
                text = fetch(url)
            except Exception:
                text = ""
            match = re.search(r"wall(-?\d+_\d+)", url)
            scope_ids = ("post" + match[1], match[1]) if match else ("__no_post__",)
            parser = PublicHTML(("wall_post_text", "pi_text", "PostText"), scope_ids=scope_ids)
            parser.feed(text)
            # A group/profile contains many posts. Only a direct wall link is a source post.
            if re.search(r"wall-?\d+_\d+", url) and parser.text:
                return {**result, "text_content": parser.text, "content_kind": "post"}
            extra = read_vk_fallback(url, timeout=max(0, deadline-time.monotonic()), allow_browser=allow_browser)
            if extra.get("text_content"):
                return {**result, **extra}
            result.update(extra)
        parser = PublicHTML()
        parser.feed(text)
        description = parser.meta.get("og:description") or parser.meta.get("description", "")
        if description and not any(w in description.casefold() for w in ("log in", "sign in", "войдите", "войти", "captcha")):
            return {**result, "text_content": description, "content_kind": "metadata", "caption_status": "not_read"}
        return {**result, "error": "guest_content_unavailable", "content_kind": "unavailable"}
    except Exception as exc:
        return {**result, "error": str(exc) if isinstance(exc, GuestTransportError) else type(exc).__name__, "content_kind": "unavailable"}
