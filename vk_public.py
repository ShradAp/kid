"""Exact-post VK guest fallbacks: configured service API and isolated JS browser."""
import os
from pathlib import Path
import re
import subprocess
import time

from guest_transport import GuestSession, configured_proxy, private_settings
from isolated_jobs import run_worker


def exact_post(value, owner, post):
    stack, inspected = [value], 0
    while stack and inspected < 10000:
        item = stack.pop()
        inspected += 1
        if isinstance(item, dict):
            if (str(item.get("owner_id")) == str(owner) and str(item.get("id", item.get("post_id"))) == str(post)
                    and isinstance(item.get("text"), str) and item["text"].strip()):
                return item["text"][:12000]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def read_vk_fallback(url, *, timeout=12, allow_browser=True):
    match = re.search(r"wall(-?\d+)_(\d+)", url)
    if not match:
        return {"vk_methods": {"guest_browser": "exact_post_required"}}
    owner, post = match.groups()
    methods = {}
    deadline = time.monotonic() + timeout
    settings = private_settings()
    token = settings.get("vk_service_token") or os.environ.get("KID_VK_SERVICE_TOKEN")
    if token:
        try:
            with GuestSession(min(5, timeout), allowed_domains=("api.vk.com",)) as session:
                response = session.post("https://api.vk.com/method/wall.getById", data={
                    "posts": f"{owner}_{post}", "access_token": token, "v": "5.199"})
                response.raise_for_status()
                value = response.json()
                text = exact_post(value.get("response", []), owner, post)
                if text:
                    return {"text_content": text, "content_kind": "post", "vk_methods": {"service_api": "read"}}
                methods["service_api"] = "api_error_" + str(value.get("error", {}).get("error_code", "no_post"))
        except Exception as exc:
            methods["service_api"] = type(exc).__name__
    else:
        methods["service_api"] = "not_configured"
    remaining = deadline - time.monotonic()
    if not allow_browser or remaining < 2:
        return {"vk_methods": {**methods, "guest_browser": "budget_skipped"}}
    try:
        payload = {"url": url, "owner": owner, "post": post, "proxy": configured_proxy(),
                   "timeout": remaining, "deadline": deadline-.5}
        result = run_worker(Path(__file__).with_name("vk_guest_worker.py"), payload, remaining)
        methods["guest_browser"] = result.get("status", "worker_failed")
        if result.get("text"):
            return {"text_content": result["text"][:12000], "content_kind": "post", "vk_methods": methods}
    except subprocess.TimeoutExpired:
        methods["guest_browser"] = "deadline"
    except (ValueError, OSError):
        methods["guest_browser"] = "worker_failed"
    return {"vk_methods": methods}
