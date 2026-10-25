"""Fetch existing captions with a free local library; never download video/ASR."""
import copy
import threading
import time


_CACHE, _LOCK = {}, threading.Lock()


def fetch_captions(video_id, session, *, max_chars=60_000, max_segments=2500):
    # Cache is scoped to the route so an IP block does not poison another proxy.
    key = (video_id, session.proxy_url, max_chars, max_segments)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and now < cached[0]:
            result = copy.deepcopy(cached[1])
            result["caption_cache"] = "hit"
            return result
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        tracks = YouTubeTranscriptApi(http_client=session).list(video_id)
        choices = list(tracks)
        choices.sort(key=lambda t: (("ru", "en").index(t.language_code) if t.language_code in ("ru", "en") else 2,
                                    t.is_generated))
        if not choices:
            return {"caption_status": "not_available"}
        track = choices[0]
        fetched = track.fetch()
        lines, count, truncated = [], 0, False
        for snippet in fetched:
            seconds = max(0, int(snippet.start))
            line = f"[{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}] {snippet.text.strip()}"
            if len(lines) >= max_segments or count + len(line) + 1 > max_chars:
                truncated = True
                break
            lines.append(line)
            count += len(line) + 1
        if not lines:
            result = {"caption_status": "empty"}
        else:
            result = {"content_kind": "transcript", "text_content": "\n".join(lines),
                      "caption_language": track.language_code, "automatic_captions": track.is_generated,
                      "caption_provider": "youtube-transcript-api", "caption_status": "available",
                      "transcript_truncated": truncated, "caption_cache": "miss"}
    except ImportError:
        result = {"caption_status": "dependency_missing"}
    except Exception as exc:
        # Library exception strings may contain request URLs/credentials.
        result = {"caption_status": type(exc).__name__}
    with _LOCK:
        if len(_CACHE) >= 128:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic() + (3600 if result.get("content_kind") == "transcript" else 60), copy.deepcopy(result))
    return result
