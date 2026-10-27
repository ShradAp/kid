"""Create a separate Codex profile using kid's saved private proxy settings."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_manager import load_config, save_config
from scripts.prepare_codex_runtime import private_endpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-app-proxy", action="store_true", required=True)
    parser.add_argument("--profile-id", default="codex")
    args = parser.parse_args()
    endpoint = private_endpoint(use_app_proxy=True)
    cfg = load_config()
    profiles = cfg.setdefault("profiles", {})
    if args.profile_id in profiles:
        print(json.dumps({"profile_already_exists": True, "settings_changed": False}))
        return
    authority = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    username = quote(endpoint.username, safe="") + "@" if endpoint.username else ""
    profiles[args.profile_id] = {"display_name": "Codex", "engine": "codex", "api_key": "", "model": "",
        "base_url": "", "system_prompt": "", "max_steps": 60, "request_timeout": 300,
        "codex": {"proxy": f"{endpoint.scheme}://{username}{authority}:{endpoint.port}",
                  "proxy_password": endpoint.password, "search": "codex", "model": "", "effort": "", "workspace": ""}}
    save_config(cfg)
    print(json.dumps({"codex_profile_created": True, "active_profile_changed": False, "other_profiles_changed": False}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Codex profile configuration failed; private details suppressed")
        raise SystemExit(1)
