"""Bounded research orchestration, independent of HTTP and model providers.

Search adapters return {items, status, error}; readers return page dictionaries.
Content is evidence, never an instruction to execute a tool or change budgets.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, asdict
import hashlib
import math
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from public_sources import platform_for, useful_candidate, scope_for, SEARCH_DOMAINS
from search_policy import CATEGORIES
from functools import lru_cache


# A cancelled browser/HTTP call cannot be killed safely. Bound outstanding work
# across ALL investigations, including calls left finishing after cancellation.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="research")
_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True)
class SearchBudget:
    max_requests: int = 12
    max_reads: int = 12
    max_rounds: int = 4
    max_pages: int = 3
    max_candidates: int = 80
    max_planner_calls: int = 3
    max_transcripts: int = 2
    max_read_seconds: float = 18.0
    max_audio_jobs: int = 1
    max_browser_reads: int = 1
    max_media_seconds: float = 25.0
    max_optional_scopes: int = 2
    max_seconds: float = 90.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid search budget: {name}")
        for name in ("max_requests", "max_reads", "max_rounds", "max_pages", "max_candidates", "max_planner_calls", "max_transcripts", "max_audio_jobs", "max_browser_reads", "max_optional_scopes"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"Expected integer: {name}")
        if not 0 <= self.max_seconds <= 600 or not 0 <= self.max_read_seconds <= 60 or not 0 <= self.max_media_seconds <= 60:
            raise ValueError("max_seconds must be between 0 and 600")


class SearchResults(list):
    """Preserve the old list/tuple contract while exposing the research report."""
    def __init__(self, items=(), report=None):
        super().__init__(items)
        self.report = report or {}


def canonical_url(url):
    """Remove tracking only; preserve semantic query parameters and URL paths."""
    try:
        p = urlsplit(url)
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password:
            return ""
        host = p.hostname.lower().rstrip(".")
        port = p.port
        if ":" in host:
            host = f"[{host}]"
        if port and (p.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                  if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "yclid"}]
        return urlunsplit((p.scheme.lower(), host, p.path or "/", urlencode(sorted(params)), ""))
    except (ValueError, TypeError):
        return ""


def relevant_fragment(text, query, limit=5000):
    """Collect several matching windows, including mentions deep in a document."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    lowered = text.lower()
    terms = {t for t in _terms(query) if len(t) >= 3 and t in lowered}
    if not terms:
        return text[:limit] + "…"
    # Rare terms and entity mentions deep in a document must survive common
    # words repeated near its beginning. Allocate separate bounded windows.
    positions = []
    for term in sorted(terms, key=lambda t: (lowered.count(t), t not in entity_terms(query), -len(t), t)):
        for pos in (lowered.find(term), lowered.rfind(term)):
            if all(abs(pos-other) > 900 for other in positions):
                positions.append(pos)
            if len(positions) >= 4:
                break
        if len(positions) >= 4:
            break
    width = max(1, (limit-15)//len(positions))
    windows = [(max(0, p-width//3), min(len(text), max(0, p-width//3)+width)) for p in sorted(positions)]
    return "\n…\n".join(text[a:b] for a, b in windows)[:limit]


def _terms(text):
    words = set(re.findall(r"[\w-]{2,}", text.lower())) - {
        "на", "по", "из", "за", "не", "что", "как", "это", "для", "the", "and", "of", "in", "to"}
    # Match names written in Russian and Latin (e.g. какаду / Kakadu).
    letters = dict(zip("абвгдеёзийклмнопрстуфхцыэ", "abvgdeezijklmnoprstufhcye"))
    letters.update({"ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ь": "", "ъ": ""})
    return words | {"".join(letters.get(c, c) for c in w) for w in words}


def _rank(item, query):
    terms = _terms(query)
    title = _terms(item.get("title", ""))
    body = _terms(item.get("body", ""))
    anchors = entity_terms(query)
    matched = bool(anchors.intersection(title | body))
    score = len(terms & title)*3 + len(terms & body)
    if anchors:
        if matched:
            score += 18
        elif anchors.intersection(_terms(urlsplit(item.get("url", "")).hostname or "")):
            score = max(1, score-8)  # A plausible primary site deserves inspection.
        else:
            score = min(-1, score-18)
    score += 2 if item.get("read_status") == "read" else 0
    return (score, item.get("read_status") == "read")


@lru_cache(maxsize=128)
def entity_terms(query):
    """Conservative name anchors; ordinary intent words must not become entities."""
    generic = {"найди", "найти", "какой", "какая", "какую", "какие", "использует", "используют", "такое", "информация", "информацию", "инфу", "расскажи", "мне", "про", "об", "есть", "нужен", "нужно", "сегодня", "последние", "свежие", "find", "search", "about", "what", "which", "does", "use", "uses", "ai", "аи", "api", "llm", "news", "today", "latest", "information", "please", "tell", "how", "is", "are", "this", "that", "best"}
    roots = ("модел", "подпис", "тариф", "нейросет", "сервис", "отзыв", "видео", "новост", "сравн", "работа", "пользоват", "документ", "review", "model", "subscription", "pricing", "error", "ошибк", "библиотек", "приложен")
    tokens = [w for w in re.findall(r"[\w-]{3,}", query.lower())
              if w not in generic and not w.startswith(roots) and not w.isdigit()]
    # Names explicitly in quotes have priority. Keep few anchors to avoid
    # turning a long conversational request into a requirement to match every word.
    quoted = re.findall(r'[«"]([^»"\n]{3,60})[»"]', query)
    selected = [w for part in (quoted[:1] or tokens[:2]) for w in re.findall(r"[\w-]{2,}", part.lower())
                if w not in generic and not w.startswith(roots) and not w.isdigit()]
    return frozenset().union(*(_terms(t) for t in selected)) if selected else frozenset()


def _followups(query, sources):
    """Conservative evidence-driven expansions, without executing page text.

    Use named phrases as literal search terms, retaining the original query.
    Platform-specific search belongs to separate adapters in stage 2.
    """
    found = set()
    original = _terms(query)
    for item in sources:
        text = item.get("title", "") + "\n" + item.get("body", "")
        if not original.intersection(_terms(text)):
            continue
        phrases = re.findall(r'[«"]([^»"\n]{4,65})[»"]', text)
        phrases += re.findall(r"\b[A-ZА-ЯЁ][\w.-]*(?:[ \t]+[A-ZА-ЯЁ0-9][\w.-]*){1,3}", text)
        for phrase in phrases:
            phrase = " ".join(re.findall(r"[\w.-]+", phrase))
            if phrase and _terms(phrase) - original and phrase.casefold() not in found:
                found.add(phrase.casefold())
                yield f'{query} "{phrase}"'[:300]


def investigate(query, *, search, read, budget=None, max_results=12,
                providers=("searxng", "serper"), cancel_event=None, progress=None, planner=None, routing=None, objective=None):
    """Search, read, expand and paginate under one explicit work budget.

    The deadline bounds waiting/scheduling. An already-running transport may
    finish later; its result is discarded and cannot mutate returned evidence.
    """
    budget = budget or SearchBudget()
    started = time.monotonic()
    deadline = started + budget.max_seconds
    report = {"query": query, "requests": 0, "reads": 0, "rounds": 0, "planner_calls": 0, "questions": [], "queries": [],
              "errors": [], "stop_reason": "exhausted", "budget": asdict(budget)}
    candidates, fingerprints = {}, {}
    documents, read_focus = {}, {}
    objective = str(objective or getattr(planner, "objective", "") or query)[:2000]
    branch_seen = {}
    queued, visited = set(), set()
    queue = deque()
    stale_rounds = 0
    stop_reason = None
    disabled = set()
    read_scopes = set()
    requested_reads = []
    report["refocused_reads"] = 0
    report["assessments"] = []
    report["research_mode"] = getattr(planner, "mode", "heuristic") if planner else "heuristic"
    refinement_providers = tuple(p for p in providers if ":" not in p and p != "youtube_public")[:2] or providers[:1]
    active_scopes = []
    optional_scopes = []
    if routing:
        report["routing"] = {"categories": list(CATEGORIES.values()), "initial": [], "optional": optional_scopes,
                             "origin": routing.get("origin", "query_policy")}

    def stopped():
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if time.monotonic() >= deadline:
            return "time_budget"
        return None

    def emit(message):
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    def bounded_call(fn, *args):
        if stopped():
            return None, stopped()
        if not _SLOTS.acquire(blocking=False):
            return None, "workers_busy"
        def work():
            try:
                return fn(*args)
            finally:
                _SLOTS.release()
        future = _POOL.submit(work)
        while not stopped():
            try:
                return future.result(timeout=min(.1, max(.001, deadline - time.monotonic()))), None
            except TimeoutError:
                if future.done():
                    return None, "TimeoutError"
            except Exception as exc:
                return None, type(exc).__name__
        # Do not cancel the future: its finally block owns the semaphore slot.
        return None, stopped()

    def enqueue(q, page=1, provider=None, priority=False):
        if page > budget.max_pages:
            return False
        added = False
        q = " ".join(q.split())[:300]
        for name in ((provider,) if provider else providers):
            if name in disabled:
                continue
            key = (name, q.casefold(), page)
            if key not in queued and key not in visited:
                added = True
                queued.add(key)
                if priority:
                    queue.appendleft((name, q, page))
                else:
                    queue.append((name, q, page))
        return added

    def activate(scope, optional=False):
        if not routing or scope not in routing["catalog"] or scope in active_scopes:
            return
        if optional and (routing.get("locked") or len(optional_scopes) >= budget.max_optional_scopes):
            return
        active_scopes.append(scope)
        (optional_scopes if optional else report["routing"]["initial"]).append(scope)
        enqueue(query, provider=routing["primary"]+":"+scope, priority=optional)
        if scope == "youtube":
            enqueue(query, provider="youtube_public", priority=optional)

    enqueue(query)
    if routing:
        for scope in routing["initial"][:4]:
            activate(scope)
        emit("Направления поиска: общий веб" + (", " + ", ".join(active_scopes) if active_scopes else ""))
    for round_index in range(budget.max_rounds):
        if stopped():
            stop_reason = stopped()
            break
        report["rounds"] = round_index + 1
        new_count = 0
        # Each round processes only the frontier present at its start.
        frontier = [] if requested_reads and report["reads"] < budget.max_reads else [queue.popleft() for _ in range(len(queue))]
        for provider, q, page in frontier:
            if stopped():
                stop_reason = stopped()
                break
            if report["requests"] >= budget.max_requests:
                stop_reason = "request_budget"
                break
            if provider in disabled:
                continue
            key = (provider, q.casefold(), page)
            visited.add(key)
            report["requests"] += 1
            emit(f"Поиск: {q} — {provider}, страница {page}")
            batch, error = bounded_call(search, provider, q, page, 10, max(.1, deadline - time.monotonic()))
            status = "error" if error else (batch or {}).get("status", "error")
            entry = {"provider": provider, "query": q, "page": page, "status": status}
            report["queries"].append(entry)
            if isinstance(batch, dict) and batch.get("retry_provider"):
                enqueue(q, page, batch["retry_provider"], priority=True)
            if error or status not in ("ok", "empty"):
                report["errors"].append({**entry, "error": error or (batch or {}).get("error", status)})
                if status == "unconfigured":
                    disabled.add(provider)
                continue
            items = (batch or {}).get("items", [])
            additions = 0
            seen_in_branch = branch_seen.setdefault((provider, q.casefold()), set())
            branch_additions = 0
            for raw in items:
                url = canonical_url(raw.get("url", ""))
                if not url or not useful_candidate(url):
                    continue
                if url not in seen_in_branch:
                    seen_in_branch.add(url)
                    branch_additions += 1
                origin = {"provider": provider, "query": q, "page": page, "url": raw["url"]}
                if url in candidates:
                    candidates[url]["discovered_by"].append(origin)
                    continue
                if len(candidates) >= budget.max_candidates:
                    continue
                candidates[url] = {"title": str(raw.get("title") or url)[:500], "url": url,
                    "body": str(raw.get("body") or raw.get("content") or "")[:8000],
                    "date": str(raw.get("date") or raw.get("publishedDate") or ""),
                    "platform": platform_for(url), "source_scope": scope_for(url), "author": str(raw.get("author") or "")[:300],
                    "discovered_by": [origin], "read_status": "snippet_only", "duplicate_of": None}
                additions += 1
            new_count += additions
            # Stop a provider's branch when it repeats a page or returns empty.
            if branch_additions and page < budget.max_pages and (batch or {}).get("has_more", True):
                enqueue(q, page + 1, provider)

        unread = sorted((s for s in candidates.values() if (s["read_status"] == "snippet_only" or s["url"] in requested_reads)
                         and _rank(s, query)[0] >= 0),
                        key=lambda s: _rank(s, query), reverse=True)
        # Inspect at least one candidate from each available public platform,
        # rather than allowing one site's many results to consume all reads.
        first, rest, scopes = [], [], set()
        for item in unread:
            scope = item.get("source_scope") or "web"
            if scope in scopes or scope in read_scopes:
                rest.append(item)
            else:
                first.append(item)
                scopes.add(scope)
        unread = first + rest
        # Spread reads between rounds so new discoveries can be inspected too.
        unread.sort(key=lambda s: s["url"] not in requested_reads)
        requested_reads = []
        allowance = min(3, budget.max_reads - report["reads"])
        for item in unread[:max(0, allowance)]:
            if stopped():
                stop_reason = stopped()
                break
            report["reads"] += 1
            read_scopes.add(item.get("source_scope") or "web")
            emit(f"Чтение источника: {item['url']}")
            if item["url"] in documents:
                text = documents[item["url"]]
                item["body"] = relevant_fragment(text, read_focus.get(item["url"], objective), limit=6000)
                item["excerpted"] = len(text) > 6000
                item["excerpt_focus"] = read_focus.get(item["url"], objective)
                report["refocused_reads"] += 1
                continue
            data, error = bounded_call(read, item["url"])
            if isinstance(data, dict):
                for diagnostic in ("transport", "vk_methods", "media_status", "caption_status", "media_download_errors"):
                    if data.get(diagnostic) is not None:
                        item[diagnostic] = data[diagnostic]
            text = (data or {}).get("text_content", "") if isinstance(data, dict) else ""
            if error or not text or data.get("error") or data.get("skipped"):
                item["read_status"] = "unavailable"
                item["read_error"] = error or (data or {}).get("error", "no_content")
                continue
            incoming_chars = len(str(text))
            text = str(text)[:100000]
            documents[item["url"]] = text
            focus = read_focus.get(item["url"], objective)
            item["body"] = relevant_fragment(text, focus, limit=6000)
            item["excerpt_focus"] = focus
            item["excerpted"] = len(text) > 6000
            item["loaded_chars"] = len(text)
            item["source_truncated"] = bool(data.get("source_truncated") or incoming_chars > 100000)
            item["content_kind"] = data.get("content_kind", "page")
            item["read_status"] = "metadata_only" if item["content_kind"] in ("metadata", "video_description") else "read"
            for field in ("author", "title", "caption_language", "automatic_captions", "caption_status", "access",
                          "caption_provider", "caption_cache", "transcript_truncated", "transport", "media_status", "vk_methods", "processed_audio_seconds",
                          "comment_evidence", "thread_truncated", "discussion_status"):
                if data.get(field) is not None:
                    if field == "title" and not data[field]:
                        continue
                    item[field] = data[field]
            item["retrieved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if item["read_status"] == "metadata_only":
                continue
            fingerprint = hashlib.sha256(re.sub(r"\W+", "", text.casefold()).encode()).hexdigest()
            if fingerprint in fingerprints:
                item["duplicate_of"] = fingerprints[fingerprint]
            else:
                fingerprints[fingerprint] = item["url"]

        sources = sorted(candidates.values(), key=lambda s: _rank(s, query), reverse=True)
        followups = []
        directed = False
        if (planner and sources and not stopped()
                and report["planner_calls"] < budget.max_planner_calls):
            report["planner_calls"] += 1
            evidence_sources = sorted(sources, key=lambda s: s["url"] not in read_focus)[:6]
            evidence = [{k: s[k] for k in ("title", "url", "body", "read_status", "source_scope", "duplicate_of",
                "content_kind", "caption_status", "automatic_captions", "transcript_truncated", "processed_audio_seconds",
                "excerpted", "source_truncated", "loaded_chars", "excerpt_focus") if k in s} for s in evidence_sources]
            for item in evidence:
                raw = documents.get(item["url"], item["body"])
                focus = read_focus.get(item["url"], objective+" "+" ".join(report["questions"]))
                item["body"] = relevant_fragment(raw, focus, limit=4000)
                item["excerpted"] = len(raw) > 4000
            if hasattr(planner, "research_state"):
                planner.research_state = {"requests_left": max(0, budget.max_requests-report["requests"]),
                    "reads_left": max(0, budget.max_reads-report["reads"]),
                    "rounds_left": budget.max_rounds-round_index-1,
                    "seconds_left": round(max(0, deadline-time.monotonic()), 1),
                    "previous_finish_rejected": report["assessments"][-1].get("finish_rejected", False) if report["assessments"] else False}
            plan, error = bounded_call(planner, query, evidence, list(report["queries"]),
                                       min(20, max(.1, deadline - time.monotonic())))
            if error:
                report["errors"].append({"provider": "planner", "error": error})
            elif isinstance(plan, dict):
                def texts(key, limit=8):
                    values = plan.get(key, [])
                    return [v[:500] for v in values[:limit] if isinstance(v, str)] if isinstance(values, list) else []
                report["questions"] = texts("questions")
                contradictions = texts("contradictions")
                valid_urls = {s["url"] for s in sources if s["read_status"] == "read" and not s["duplicate_of"] and _rank(s, query)[0] >= 0}
                supports = [u for u in texts("evidence_urls") if u in valid_urls]
                raw_findings = plan.get("findings", [])
                findings = [{"claim": f["claim"][:500], "urls": [u for u in f["urls"][:4] if isinstance(u, str) and u in valid_urls]}
                            for f in raw_findings[:12] if isinstance(f, dict) and isinstance(f.get("claim"), str) and f["claim"].strip()
                            and isinstance(f.get("urls"), list)] if isinstance(raw_findings, list) else []
                findings = [f for f in findings if f["urls"]]
                report["findings"] = findings
                decision = plan.get("decision", "search")
                # A model's confidence alone cannot turn a snippet into evidence.
                accepted = (decision == "finish" and bool(supports) and any(set(f["urls"]) & set(supports) for f in findings)
                            and not report["questions"] and not contradictions
                            and plan.get("questions") == [] and plan.get("contradictions") == [])
                report["assessments"].append({"decision": str(decision)[:30], "assessment": str(plan.get("assessment", ""))[:600],
                    "questions": list(report["questions"]), "contradictions": contradictions,
                    "evidence_urls": supports, "finish_rejected": decision == "finish" and not accepted})
                emit("Оценка материалов: " + ("ответ подтверждён" if accepted else "поиск оставшихся пробелов"))
                if accepted:
                    stop_reason = "sufficient_evidence"
                    break
                requested_reads = [u for u in texts("read_urls", 3) if u in candidates and candidates[u]["read_status"] == "snippet_only"]
                requests = plan.get("read_requests", [])
                if isinstance(requests, list):
                    for request in requests[:3]:
                        if not isinstance(request, dict):
                            continue
                        url, focus = canonical_url(request.get("url")), request.get("focus")
                        if url in candidates and isinstance(focus, str) and focus.strip() and read_focus.get(url) != focus[:500]:
                            read_focus[url] = focus[:500]
                            if url not in requested_reads:
                                requested_reads.append(url)
                # Legacy read_urls can also request a different passage using current gaps.
                for url in texts("read_urls", 3):
                    focus = " ".join(report["questions"])
                    if url in documents and focus and read_focus.get(url) != focus:
                        read_focus[url] = focus[:500]
                        if url not in requested_reads:
                            requested_reads.append(url)
                directed = bool(requested_reads)
                actions = plan.get("actions", [])
                if isinstance(actions, list):
                    for action in reversed(actions[:3]):
                        if not isinstance(action, dict) or not isinstance(action.get("query"), str) or not action["query"].strip():
                            continue
                        scope = action.get("source", "web")
                        if scope == "web":
                            directed = enqueue(action["query"], provider=refinement_providers[0], priority=True) or directed
                        elif isinstance(scope, str) and routing and scope in routing["catalog"]:
                            activate(scope, optional=True)
                            if scope in active_scopes:
                                directed = enqueue(action["query"], provider=routing["primary"]+":"+scope, priority=True) or directed
                if isinstance(plan.get("sources"), list):
                    for scope in plan["sources"][:2]:
                        if isinstance(scope, str):
                            activate(scope, optional=True)
                proposed = plan.get("queries", [])
                if isinstance(proposed, list):
                    followups = [q.strip()[:300] for q in proposed[:3] if isinstance(q, str) and q.strip()]
                questions = plan.get("questions", [])
                if isinstance(questions, list):
                    report["questions"] = [q[:300] for q in questions[:8] if isinstance(q, str)]
        if not followups and not directed:
            followups = []
            for candidate in _followups(query, sources):
                if any((p, candidate.casefold(), 1) not in queued for p in refinement_providers if p not in disabled):
                    followups.append(candidate)
                if len(followups) >= 3:
                    break
        # Reserve the next round for evidence-driven queries as well as paging.
        # Repeating every refinement across all site scopes would consume the
        # budget before any of them can actually be investigated.
        for followup in reversed(followups):
            for provider in reversed(refinement_providers):
                enqueue(followup, provider=provider, priority=True)
        if stopped():
            stop_reason = stopped()
        if stop_reason == "request_budget" and requested_reads and report["reads"] < budget.max_reads and not stopped():
            # Searching may be exhausted while cached passages can still be read.
            queue.clear()
            stop_reason = None
        if stop_reason:
            break
        if len(candidates) >= budget.max_candidates:
            stop_reason = "candidate_budget"
            break
        stale_rounds = stale_rounds + 1 if new_count == 0 else 0
        if stale_rounds >= 2:
            stop_reason = "no_new_sources"
            break
        if not queue and not requested_reads:
            stop_reason = "exhausted"
            break
    else:
        stop_reason = "round_budget"

    ranked = sorted(candidates.values(), key=lambda s: _rank(s, query), reverse=True)
    selected, hosts = [], {}
    remaining = [s for s in ranked if not s["duplicate_of"] and _rank(s, query)[0] >= 0]
    # Diversity is a small preference, not a reason to promote irrelevant sites
    # ahead of a second useful page on the same domain.
    while remaining:
        item = max(remaining, key=lambda s: _rank(s, query)[0] - 3*hosts.get(urlsplit(s["url"]).hostname, 0))
        remaining.remove(item)
        host = urlsplit(item["url"]).hostname
        hosts[host] = hosts.get(host, 0)+1
        selected.append(item)
    for item in candidates.values():
        item["relevance"] = "weak" if _rank(item, query)[0] < 0 else "query_match"
    if stop_reason == "sufficient_evidence":
        supporting = set(report["assessments"][-1]["evidence_urls"])
        selected.sort(key=lambda s: s["url"] not in supporting)
    elif read_focus:
        selected.sort(key=lambda s: s["url"] not in read_focus)
    report.update(stop_reason=stop_reason or "exhausted", elapsed_seconds=round(time.monotonic()-started, 3),
                  candidates=len(candidates), sources=list(candidates.values()))
    coverage = {}
    for scope in SEARCH_DOMAINS:
        attempts = [q for q in report["queries"] if q["provider"].endswith(":" + scope)
                    or (scope == "youtube" and q["provider"] == "youtube_public")]
        sources = [s for s in candidates.values() if s.get("source_scope") == scope]
        if not attempts and not sources:
            continue
        coverage[scope] = {"attempts": len(attempts), "found": len(sources),
                           "read": sum(s["read_status"] == "read" for s in sources),
                           "metadata": sum(s["read_status"] == "metadata_only" for s in sources),
                           "status": "public_results_found" if sources else (
                               "unavailable" if any(q["status"] not in ("ok", "empty") for q in attempts) else "no_indexed_results")}
    report["coverage"] = coverage
    emit(f"Поиск завершён: {len(candidates)} источников; {report['stop_reason']}")
    return SearchResults(selected[:max(1, min(int(max_results), 80))], report)
