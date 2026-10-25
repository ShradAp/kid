"""Single-video worker; parent kills this process when its wall-clock budget ends."""
import json
from pathlib import Path
import re
import sys
import time


class QuietLogger:
    def debug(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def stamp(seconds):
    seconds = max(0, int(float(seconds)))
    return f"{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}"


def media_matches_query(query, metadata):
    from research_search import _terms
    terms = _terms(query) - {"video", "видео", "найди", "информация", "инфу", "ai", "ии"}
    words = _terms(metadata)
    return bool(terms.intersection(words) or any(
        min(len(term), len(word)) >= 5 and (term.startswith(word) or word.startswith(term))
        for term in terms for word in words))


def parse_subtitles(text, extension, max_chars=30000):
    lines = []
    if extension == "json":
        data = json.loads(text)
        for item in data.get("utterances", data.get("captions", [])):
            if item.get("text"):
                lines.append(f"[{stamp(float(item.get('start_time', item.get('start', 0)))/1000)}] {item['text']}")
    else:
        current = "00:00:00"
        for line in text.splitlines():
            if "-->" in line:
                current = line.split("-->")[0].strip().replace(",", ".").split(".")[0]
            elif line.strip() and not line.strip().isdigit() and not line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
                clean = re.sub(r"<[^>]*>", "", line).strip()
                if clean:
                    lines.append(f"[{current}] {clean}")
    return "\n".join(lines)[:max_chars]


def select_audio(info):
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or not 0 < duration <= 180:
        return None, "duration_limit_or_unknown"
    # TikTok's yt-dlp 'audio' format comes from music.playUrl: it can be only
    # background music. Speech must come from the audio track of the video.
    def estimated_size(f):
        return f.get("filesize") or f.get("filesize_approx") or ((f.get("tbr") or 0)*1000*duration/8) or 0
    formats = [f for f in info.get("formats", []) if f.get("url") and f.get("acodec") != "none"
               and f.get("format_id") != "audio" and f.get("vcodec") != "none"
               and f.get("protocol", "https") in ("http", "https")
               and estimated_size(f) <= 8_000_000]
    formats.sort(key=lambda f: (estimated_size(f) or 8_000_000,
                               (f.get("width") or 10000)*(f.get("height") or 10000), f.get("tbr") or 10000))
    return (formats[0], "selected") if formats else (None, "no_bounded_media_format")


def decode_audio_bounded(path, deadline, seconds=90):
    """Decode only the audio stream and only a bounded number of PCM samples."""
    import av
    import numpy as np
    chunks, samples, maximum = [], 0, int(seconds * 16000)
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            if time.monotonic() >= deadline:
                raise TimeoutError("audio_decode_deadline")
            for audio in resampler.resample(frame):
                chunk = audio.to_ndarray().reshape(-1)[:maximum-samples]
                chunks.append(chunk)
                samples += len(chunk)
            if samples >= maximum:
                break
    return np.concatenate(chunks).astype(np.float32)/32768.0 if chunks else np.zeros(0, dtype=np.float32)


def run(payload):
    from yt_dlp import YoutubeDL
    from guest_transport import GuestSession
    url = payload["url"]
    if not re.fullmatch(r"https://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+(?:\?[^\s]*)?", url):
        return {"media_status": "single_video_url_required"}
    deadline = payload.get("deadline", time.monotonic() + payload["timeout"])
    options = {"quiet": True, "no_warnings": True, "logger": QuietLogger(), "noplaylist": True,
               "writesubtitles": True,
               "skip_download": True, "socket_timeout": 3, "retries": 0, "extractor_retries": 0,
               "fragment_retries": 0, "proxy": payload.get("proxy", ""), "cachedir": False,
               "usenetrc": False, "cookiefile": None, "cookiesfrombrowser": None}
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        anonymous_cookies = list(ydl.cookiejar)
    if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video"):
        return {"media_status": "single_video_required"}
    domains = ("tiktok.com", "tiktokv.com", "tiktokcdn.com", "tiktokcdn-us.com", "byteoversea.com", "ibytedtos.com", "muscdn.com")
    with GuestSession(max(.1, deadline-time.monotonic()), proxy_url=payload.get("proxy", ""),
                      allowed_domains=domains, max_requests=4, max_bytes=8_000_000, max_response_bytes=8_000_000,
                      anonymous_cookies=anonymous_cookies) as session:
        for key, value in (info.get("http_headers") or {}).items():
            if key.lower() in ("user-agent", "referer", "accept"):
                session.headers[key] = value
        subtitles = info.get("subtitles") or {}
        for lang in sorted(subtitles, key=lambda l: (not l.startswith("ru"), not l.startswith("en"))):
            for sub in subtitles[lang][:2]:
                try:
                    if sub.get("data"):
                        text = sub["data"]
                    elif sub.get("url"):
                        response = session.get(sub["url"])
                        response.raise_for_status()
                        text = response.text
                    else:
                        continue
                    text = parse_subtitles(text, sub.get("ext", "vtt"), max_chars=30001)
                    if text:
                        return {"content_kind": "transcript", "text_content": text[:30000], "caption_language": lang,
                                "transcript_truncated": len(text)>30000,
                                "caption_provider": "yt-dlp", "media_status": "captions_available"}
                except Exception:
                    continue
        if not payload.get("allow_audio"):
            return {"media_status": "captions_unavailable"}
        # Only process a relevant selected result, never crawl profiles or follow authors.
        if not media_matches_query(payload.get("query", ""), str(info.get("title", ""))+" "+str(info.get("description", ""))):
            return {"media_status": "audio_skipped_low_relevance"}
        selected, reason = select_audio(info)
        if selected is None:
            return {"media_status": reason}
        # No implicit model download in the search path. The model is prepared separately.
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(payload.get("model", "tiny"), device="cpu", compute_type="int8",
                                 local_files_only=True, cpu_threads=2, num_workers=1)
        except Exception:
            return {"media_status": "local_speech_model_unavailable"}
        if time.monotonic() >= deadline:
            return {"media_status": "deadline"}
        response = None
        failures = []
        for _ in range(2):
            try:
                response = session.get(selected["url"])
                response.raise_for_status()
                break
            except Exception as exc:
                from guest_transport import GuestTransportError
                failures.append({"error": str(exc) if isinstance(exc, GuestTransportError) else type(exc).__name__,
                                 "http_status": getattr(getattr(exc, "response", None), "status_code", None)})
                if isinstance(exc, GuestTransportError) and str(exc) == "byte_limit":
                    return {"media_status": "media_size_limit", "media_download_errors": failures}
                remaining_formats = [f for f in info.get("formats", []) if f.get("url") != selected["url"]]
                selected, _ = select_audio({**info, "formats": remaining_formats})
                if selected is None:
                    return {"media_status": "media_download_unavailable", "media_download_errors": failures}
        if response is None or not response.ok:
            return {"media_status": "media_download_unavailable", "media_download_errors": failures}
        media = Path(payload["work_dir"]) / "audio.media"
        media.write_bytes(response.content)
        waveform = decode_audio_bounded(media, deadline)
        segments, metadata = model.transcribe(waveform, beam_size=1, vad_filter=True,
                                              condition_on_previous_text=False, clip_timestamps="0,90")
        lines = []
        for segment in segments:
            if time.monotonic() >= deadline:
                return {"media_status": "deadline"}
            lines.append(f"[{stamp(segment.start)}] {segment.text.strip()}")
            if len(lines) >= 500:
                break
        joined = "\n".join(lines)
        text = joined[:12_000]
        return {"content_kind": "speech_transcript", "text_content": text,
                "caption_language": metadata.language, "caption_provider": "local-whisper",
                "media_status": "audio_transcribed" if text else "no_speech", "automatic_captions": True,
                "transcript_truncated": info["duration"] > 90 or len(joined)>12000 or len(lines)>=500,
                "processed_audio_seconds": round(min(len(waveform)/16000, 90), 2)}


if __name__ == "__main__":
    try:
        result = run(json.loads(sys.stdin.read()))
    except Exception as exc:
        result = {"media_status": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))
