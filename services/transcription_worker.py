"""Provider-independent URL/subtitle/audio reader. Credentials stay in this job."""
import io
import json
import os
from pathlib import Path
import re
import sys
import time


class Quiet:
    def debug(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def parse_captions(raw, extension):
    """Keep the source verbatim on disk; decode common text/JSON captions."""
    if extension in ("json", "json3"):
        data = json.loads(raw)
        segments = []
        for item in data.get("events", data.get("utterances", data.get("captions", []))):
            text = item.get("text") or "".join(v.get("utf8", "") for v in item.get("segs", []))
            start = item.get("tStartMs", item.get("start_time", item.get("start", 0))) / 1000
            end = start + item.get("dDurationMs", item.get("duration", 0)) / 1000
            if text.strip():
                segments.append({"start": round(start, 3), "end": round(end, 3), "text": text.strip()})
        return segments
    def seconds(value):
        pieces = value.replace(",", ".").split(":")
        return sum(float(part) * 60 ** index for index, part in enumerate(reversed(pieces)))
    segments, current = [], None
    for line in raw.splitlines():
        if "-->" in line:
            if current and current["text"].strip():
                segments.append(current)
            start, end = line.split("-->", 1)
            current = {"start": seconds(start.strip()), "end": seconds(end.strip().split()[0]), "text": ""}
        elif not line.strip():
            if current and current["text"].strip():
                segments.append(current)
            current = None
        elif current:
            import html
            text = html.unescape(re.sub(r"<[^>]*>", "", line)).strip()
            current["text"] += (" " if current["text"] else "") + text
    if current and current["text"].strip():
        segments.append(current)
    return segments


def public_metadata(info):
    # Signed media URLs, request headers, cookies and account tokens are not
    # video content and must never enter the archive or the model context.
    keys = ("id", "title", "description", "duration", "uploader", "channel", "upload_date",
            "timestamp", "tags", "categories", "chapters", "language", "license", "availability", "extractor")
    return {key: info[key] for key in keys if key in info}


def run(payload):
    from guest_transport import GuestSession, GuestTransportError
    from net_guard import is_safe_target
    from yt_dlp import YoutubeDL
    from yt_dlp.networking import Request, Response
    from yt_dlp.networking.exceptions import HTTPError, RequestError
    import urllib.request
    deadline = time.monotonic() + payload["timeout"]
    url = payload["url"]
    if not is_safe_target(url):
        return {"status": "unavailable", "reason": "unsafe_target"}
    proxy = payload["proxy"]
    with GuestSession(timeout=min(40, payload["timeout"]), proxy_url=proxy, max_requests=40,
                      max_bytes=16_000_000, max_response_bytes=8_000_000) as metadata_session:
        metadata_session.strict_proxy = bool(proxy)

        class GuardedDownloader(YoutubeDL):
            def urlopen(self, request):
                if isinstance(request, str):
                    request = Request(request)
                elif isinstance(request, urllib.request.Request):
                    request = Request(request.full_url, data=request.data, headers=dict(request.header_items()),
                                      method=request.get_method())
                try:
                    headers = dict(self.params.get("http_headers") or {})
                    headers.update(request.headers)
                    # Only this fresh guest session's cookie jar is used.
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("cookie", "authorization", "proxy-authorization")}
                    for cookie in self.cookiejar:
                        metadata_session.cookies.set_cookie(cookie)
                    fetched = metadata_session.request(request.method, request.url, data=request.data, headers=headers)
                    for cookie in metadata_session.cookies:
                        self.cookiejar.set_cookie(cookie)
                    response = Response(io.BytesIO(fetched.content), url=fetched.url, headers=dict(fetched.headers),
                                        status=fetched.status_code, reason=fetched.reason)
                    if fetched.status_code >= 400:
                        raise HTTPError(response)
                    return response
                except GuestTransportError as error:
                    raise RequestError(str(error)) from None

        with GuardedDownloader({"quiet": True, "no_warnings": True, "logger": Quiet(), "skip_download": True,
                                "noplaylist": True, "cachedir": False, "usenetrc": False, "cookiefile": None,
                                "cookiesfrombrowser": None, "proxy": proxy, "socket_timeout": 5,
                                "retries": 0, "extractor_retries": 0, "fragment_retries": 0}) as downloader:
            info = downloader.extract_info(url, download=False)
            cookies = list(downloader.cookiejar)
        diagnostics = metadata_session.diagnostics()
    if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video"):
        return {"status": "unavailable", "reason": "single_media_required"}
    metadata = public_metadata(info)
    base = {"url": url, "canonical_url": info.get("webpage_url", url), "metadata": metadata,
            "metadata_transport": diagnostics, "quality": payload["quality"]}
    duration = info.get("duration")
    if info.get("is_live") or not isinstance(duration, (int, float)) or not 0 < duration <= payload["max_duration"]:
        return {**base, "status": "unavailable", "reason": "duration_limit_or_unknown"}
    captions = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
    base["available_caption_languages"] = sorted(captions)
    preferred = payload.get("language")
    with GuestSession(timeout=max(.1, min(60, deadline-time.monotonic())), proxy_url=proxy, max_requests=30,
                      max_bytes=payload["max_bytes"], max_response_bytes=payload["max_bytes"],
                      anonymous_cookies=cookies) as session:
        session.strict_proxy = bool(proxy)
        for language in sorted(captions, key=lambda lang: (lang != preferred, not lang.startswith("en"))):
            for caption in captions[language][:2]:
                if caption.get("ext", "vtt") not in ("vtt", "srt", "json", "json3"):
                    continue
                try:
                    response = session.get(caption["url"])
                    response.raise_for_status()
                    segments = parse_captions(response.text, caption.get("ext", "vtt"))
                    if segments:
                        return {**base, "status": "read", "content_kind": "transcript", "provider": "published_captions",
                                "language": language, "automatic": language not in (info.get("subtitles") or {}),
                                "segments": segments, "text_content": "\n".join(v["text"] for v in segments),
                                "raw_subtitles": response.text, "subtitle_format": caption.get("ext", "vtt"),
                                "transport": session.diagnostics(), "transcript_truncated": False}
                except Exception:
                    pass
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(payload["model"], device="cpu", compute_type="int8", local_files_only=True,
                                 cpu_threads=min(4, os.cpu_count() or 2), num_workers=1)
        except Exception:
            return {**base, "status": "unavailable", "reason": "local_speech_model_unavailable", "model": payload["model"]}
        formats = [f for f in info.get("formats", []) if f.get("url") and f.get("acodec") != "none"
                   and f.get("protocol", "https") in ("http", "https")]
        def size(item):
            return item.get("filesize") or item.get("filesize_approx") or (item.get("tbr") or 0) * 125 * duration
        formats.sort(key=lambda f: (f.get("vcodec") == "none", size(f) or payload["max_bytes"]))
        formats = [f for f in formats if size(f) <= payload["max_bytes"]]
        if not formats:
            return {**base, "status": "unavailable", "reason": "no_bounded_audio_format"}
        response = None
        for selected in formats[:3]:
            try:
                candidate = session.get(selected["url"])
                candidate.raise_for_status()
                response = candidate
                base["audio_selection"] = {key: selected[key] for key in
                    ("format_id", "acodec", "vcodec", "abr", "tbr", "asr", "audio_channels", "filesize", "filesize_approx")
                    if key in selected}
                break
            except Exception:
                pass
        if response is None:
            return {**base, "status": "unavailable", "reason": "media_download_failed", "transport": session.diagnostics()}
        import av
        import numpy as np
        chunks, count, maximum = [], 0, payload["max_duration"] * 16000
        with av.open(io.BytesIO(response.content)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                if time.monotonic() >= deadline:
                    return {**base, "status": "unavailable", "reason": "transcription_deadline"}
                for audio in resampler.resample(frame):
                    data = audio.to_ndarray().reshape(-1)[:maximum-count]
                    chunks.append(data)
                    count += len(data)
                if count >= maximum:
                    break
            for audio in resampler.resample(None):
                data = audio.to_ndarray().reshape(-1)[:maximum-count]
                chunks.append(data)
                count += len(data)
        if not count:
            return {**base, "status": "unavailable", "reason": "no_audio"}
        segments, details = model.transcribe(np.concatenate(chunks).astype(np.float32)/32768,
            language=payload.get("language"), initial_prompt=payload.get("vocabulary") or None,
            beam_size=5 if payload["quality"] == "accurate" else 3,
            vad_filter=True, condition_on_previous_text=False, word_timestamps=True)
        lines = []
        for segment in segments:
            if time.monotonic() >= deadline:
                break
            lines.append({"start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip(),
                "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob,
                "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "probability": w.probability}
                          for w in segment.words or []]})
        truncated = time.monotonic() >= deadline or count >= maximum
        return {**base, "status": "read" if lines else "unavailable", "reason": "" if lines else "no_speech",
                "content_kind": "speech_transcript", "provider": "local-whisper", "model": payload["model"],
                "language": details.language, "language_probability": details.language_probability,
                "automatic": True, "segments": lines, "text_content": "\n".join(v["text"] for v in lines),
                "processed_audio_seconds": round(count/16000, 3), "transcript_truncated": truncated,
                "transport": session.diagnostics()}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    payload = json.loads(sys.stdin.buffer.read(32768).decode("utf-8"))
    try:
        output = run(payload)
    except Exception as error:
        # Third-party error strings may contain signed URLs or proxy passwords.
        output = {"status": "unavailable", "reason": type(error).__name__}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=True).encode("utf-8"))
