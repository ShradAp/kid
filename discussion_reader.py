"""Bounded public thread reading with separate links for comment evidence."""
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urljoin

from guest_transport import GuestSession
import requests


def _bundle(url, title, body, comments, truncated=False, status="read"):
    truncated = truncated or len(body) > 12000 or any(c.get("truncated") for c in comments)
    comments = comments[:30]
    parts = [title, body[:12000]]
    for comment in comments:
        parts.append(f"Комментарий: {comment['url']}\n{comment['text'][:2000]}")
    return {"url": url, "title": title, "text_content": "\n\n".join(parts)[:80000],
            "content_kind": "discussion", "comment_evidence": comments,
            "thread_truncated": truncated, "discussion_status": status}


def _github(url, session, match):
    owner, repo, number = match.groups()
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    response = session.get(base)
    response.raise_for_status()
    issue = response.json()
    total = max(0, int(issue.get("comments", 0)))
    comments = []
    status = "read"
    pages = ([1] if total <= 15 else [1, (total+14)//15]) if total else []
    for page in pages:
        try:
            response = session.get(base+"/comments?"+urlencode({"per_page": 15, "page": page}))
            response.raise_for_status()
            batch = response.json()
            for item in batch:
                text = item.get("body") or ""
                if text.strip():
                    comments.append({"url": f"https://github.com/{owner}/{repo}/issues/{number}#issuecomment-{int(item['id'])}",
                                     "text": text[:2000], "truncated": len(text)>2000})
        except Exception as exc:
            status = "comments_"+type(exc).__name__
            break
    return _bundle(url, issue.get("title", ""), issue.get("body") or "", comments, len(comments) < total, status)


def _reddit(url, session, match):
    sub, post = match.groups()
    base = f"https://www.reddit.com/r/{sub}/comments/{post}"
    response = session.get(base+".json?limit=30&depth=3&raw_json=1")
    response.raise_for_status()
    data = response.json()
    root = data[0]["data"]["children"][0]["data"]
    comments, stack, truncated = [], list(reversed(data[1]["data"]["children"])), False
    while stack and len(comments) < 30:
        item = stack.pop()
        if item.get("kind") == "more":
            truncated = True
            continue
        node = item.get("data", {})
        body = node.get("body", "")
        if body and body not in ("[deleted]", "[removed]"):
            identifier = str(node.get("id", ""))
            if re.fullmatch(r"[a-z0-9]+", identifier):
                comments.append({"url": base+"/_/"+identifier+"/", "text": body[:2000], "truncated": len(body)>2000})
        replies = node.get("replies")
        if isinstance(replies, dict):
            stack.extend(reversed(replies.get("data", {}).get("children", [])))
    truncated = truncated or bool(stack) or int(root.get("num_comments", 0)) > len(comments)
    return _bundle(url, root.get("title", ""), root.get("selftext", ""), comments, truncated)


class ThreadHTML(HTMLParser):
    """Only known post/comment bodies; no menus, author profiles or sidebars."""
    CLASSES = {"postcolor", "post_body", "tm-comment__body-content", "comment__message", "tm-article-body", "article-formatted-body"}
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.stack, self.records, self.current = base, [], [], None
        self.pages = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "a" and attrs.get("href"):
            candidate = urlsplit(urljoin(self.base, attrs["href"]))
            origin = urlsplit(self.base)
            query = parse_qs(candidate.query)
            if candidate.hostname == origin.hostname == "4pda.to" and query.get("showtopic") == parse_qs(origin.query).get("showtopic"):
                offset = query.get("st", [""])[0]
                if offset.isdigit():
                    self.pages.add(int(offset))
        if tag in ("br", "hr"):
            if self.current:
                self.current["parts"].append("\n")
            return
        if tag in ("img", "input", "meta", "link", "source", "wbr"):
            return
        identifier = attrs.get("id", "")
        active_id = identifier or (self.stack[-1][1] if self.stack else "")
        blocked = tag in ("script", "style", "noscript") or (self.stack and self.stack[-1][2])
        self.stack.append((tag, active_id, bool(blocked)))
        if self.current is None and self.CLASSES.intersection(attrs.get("class", "").split()):
            self.current = {"depth": len(self.stack), "id": active_id, "parts": []}

    def handle_data(self, text):
        if self.current and not self.stack[-1][2]:
            self.current["parts"].append(text)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] != tag:
                continue
            if self.current and i < self.current["depth"]:
                text = "".join(self.current["parts"]).strip()
                if text:
                    anchor = self.current["id"]
                    if urlsplit(self.base).hostname == "habr.com" and anchor.isdigit():
                        anchor = "comment_"+anchor
                    if re.fullmatch(r"(?:comment_|entry|post)[\w-]*\d+", anchor):
                        link = self.base.split("#")[0]+"#"+anchor
                    else:
                        link = self.base
                    self.records.append({"url": link, "text": text[:4000]})
                self.current = None
            del self.stack[i:]
            break


def read_discussion(url, *, timeout=12):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    gh = re.fullmatch(r"/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)/?", parts.path) if host == "github.com" else None
    reddit = re.match(r"/r/([\w]+)/comments/([a-z0-9]+)(?:/|$)", parts.path) if host in ("reddit.com", "www.reddit.com", "old.reddit.com") else None
    html_thread = host in ("4pda.to", "habr.com") and (
        "showtopic" in parse_qs(parts.query) if host == "4pda.to" else bool(re.search(r"/(?:articles|posts)/\d+", parts.path)))
    if not (gh or reddit or html_thread):
        return None
    domains = ("api.github.com",) if gh else (("reddit.com",) if reddit else (host,))
    session = None
    try:
        with GuestSession(timeout, allowed_domains=domains, max_requests=3, max_bytes=3_000_000) as session:
            if gh:
                result = _github(url, session, gh)
            elif reddit:
                result = _reddit(url, session, reddit)
            else:
                response = session.get(url)
                response.raise_for_status()
                parser = ThreadHTML(url)
                parser.feed(response.text)
                if host == "habr.com" and "/comments" not in parts.path:
                    try:
                        comments_url = "https://habr.com"+parts.path.rstrip("/")+"/comments/"
                        response = session.get(comments_url)
                        response.raise_for_status()
                        comments_parser = ThreadHTML(comments_url)
                        comments_parser.feed(response.text)
                        parser.records.extend(record for record in comments_parser.records
                                              if "#comment_" in record["url"])
                    except Exception:
                        pass
                if not parser.records:
                    return {"discussion_status": "comments_not_exposed"}
                if host == "4pda.to" and parser.pages:
                    offset = max(parser.pages)
                    current = int(parse_qs(parts.query).get("st", ["0"])[0])
                    if offset != current:
                        try:
                            latest_url = "https://4pda.to"+parts.path+"?"+urlencode({"showtopic": parse_qs(parts.query)["showtopic"][0], "st": offset})
                            response = session.get(latest_url)
                            response.raise_for_status()
                            latest = ThreadHTML(latest_url)
                            latest.feed(response.text)
                            parser.records = parser.records[:16]+latest.records[-15:]
                        except Exception:
                            pass
                result = _bundle(url, "", parser.records[0]["text"], parser.records[1:31], True)
            result["transport"] = session.diagnostics()
            return result
    except Exception as exc:
        status = "HTTP_"+str(exc.response.status_code) if isinstance(exc, requests.HTTPError) and exc.response is not None else type(exc).__name__
        return {"discussion_status": status, **({"transport": session.diagnostics()} if session else {})}
