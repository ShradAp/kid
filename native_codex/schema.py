"""Shared kid tool descriptions for MCP and App Server client tools."""
import copy
from api_client import TOOLS_SCHEMA
from kid_mcp_server import HOST_TOOLS, NETWORK_TOOLS


def tools(search):
    result = [{"name": item["function"]["name"], "description": item["function"]["description"],
               "inputSchema": item["function"]["parameters"]} for item in TOOLS_SCHEMA]
    result.extend(HOST_TOOLS)
    result = copy.deepcopy(result)
    windows = next(item for item in result if item["name"] == "windows_command")
    windows["description"] = "Execute a command on the user's real Windows PC after approval. Normal host network is allowed. PowerShell by default, or cmd. Returns actual stdout and exit code."
    windows["inputSchema"] = {"type": "object", "properties": {"command": {"type": "string"},
        "shell": {"type": "string", "enum": ["powershell", "cmd"]}, "cwd": {"type": "string"}},
        "required": ["command"], "additionalProperties": False}
    return [item for item in result if search == "kid" or item["name"] not in NETWORK_TOOLS]
