"""Small stdio MCP server inside the isolated worker (no external transport)."""
import argparse
import http.client
import json
from pathlib import Path
import sys
import os

NETWORK_TOOLS = {"web_search", "fetch_webpage", "analyze_github_repo"}
HOST_TOOLS = [
    {"name":"windows_command", "description":"Execute an explicitly supported local diagnostic on the user's real Windows PC. General cmd/PowerShell and network-changing arguments are blocked. Returns the actual Windows output.",
     "inputSchema":{"type":"object","properties":{"command":{"type":"string","enum":["ipconfig","ipconfig /all"]}},"required":["command"],"additionalProperties":False}},
    {"name":"windows_write_file", "description":"Create a new file on the user's real Windows Desktop or selected Windows project. Supported formats: txt, md, csv, json, docx. Never overwrites an existing file. Returns the real host path; do not invent a container Desktop.",
     "inputSchema":{"type":"object","properties":{"file_name":{"type":"string"},"content":{"type":"string"},"location":{"type":"string","enum":["desktop","project"]}},"required":["file_name","content","location"],"additionalProperties":False}},
]


def main():
    # Native Windows pipes otherwise use a legacy code page. MCP is UTF-8.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--search", choices=("kid", "codex"), required=True)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    if args.native:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from native_codex.schema import tools as native_tools
        tools = native_tools(args.search)
    else:
        tools = json.loads(Path("/opt/kid/kid_tools.json").read_text(encoding="utf-8"))
        tools.extend(HOST_TOOLS)
    if args.search == "codex":
        tools = [item for item in tools if item["name"] not in NETWORK_TOOLS]
    allowed = {item["name"] for item in tools}
    for line in sys.stdin:
        request = None
        if len(line) > 2 * 1024 * 1024:
            break
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method, params = request.get("method"), request.get("params") or {}
            if method == "initialize":
                version = params.get("protocolVersion")
                if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                    version = "2025-06-18"
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "kid", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                if params.get("name") not in allowed or not isinstance(params.get("arguments", {}), dict):
                    raise ValueError()
                connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=300)
                try:
                    payload = {"session_id": args.session, "name": params["name"], "arguments": params.get("arguments", {})}
                    headers = {"Content-Type": "application/json"}
                    if args.native:
                        headers["Authorization"] = "Bearer " + os.environ.get("KID_MCP_TOKEN", "")
                    connection.request("POST", "/invoke", body=json.dumps(payload), headers=headers)
                    response = connection.getresponse()
                    result = json.loads(response.read(256 * 1024))
                    if response.status != 200:
                        raise ValueError()
                finally:
                    connection.close()
            else:
                raise ValueError()
            answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception:
            answer = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                      "error": {"code": -32602, "message": "Tool unavailable or request rejected"}}
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
