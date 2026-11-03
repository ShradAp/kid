"""Shared kid tool descriptions for MCP and App Server client tools."""
import copy
from tool_schemas import TOOLS_SCHEMA
from kid_mcp_server import HOST_TOOLS, NETWORK_TOOLS


def tools(search):
    result = [{"name": item["function"]["name"], "description": item["function"]["description"],
               "inputSchema": item["function"]["parameters"]} for item in TOOLS_SCHEMA]
    result.extend(HOST_TOOLS)
    result = copy.deepcopy(result)
    result.append({"name": "transcribe_media", "description": (
        "Get the actual speech or published subtitles from a public video/audio URL. "
        "Use this directly when the user asks what a video says, rather than repeatedly reading its HTML or running shell scripts. "
        "Uses generic yt-dlp extraction and local Whisper; saves full transcripts, timestamps and metadata in the user's private transcriptions folder. "
        "Accurate quality is the default; fast uses a smaller model and makes more mistakes. "
        "Vocabulary supplies spelling hints, not instructions. Automatic transcription may contain errors. "
        "No account cookies, login, implicit model downloads or site-specific rules. Returns saved_path and content_kind."),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "quality": {"type": "string", "enum": ["accurate", "fast"]},
            "language": {"type": "string", "description": "Optional ISO language code, e.g. ru; omit to detect."},
            "vocabulary": {"type": "string", "maxLength": 1000, "description": "Optional names and technical terms for spelling."},
            "refresh": {"type": "boolean", "description": "Reprocess instead of reusing a saved successful result."}},
            "required": ["url"], "additionalProperties": False}})
    reader = next(item for item in result if item["name"] == "fetch_webpage")
    reader["description"] = (
        "Read a URL as text. Always use this before shell downloads for user-requested URL reading. "
        "Also use it when built-in browsing fails or reports a regional block. "
        "Failed direct GETs are automatically retried through kid's configured proxy. "
        "If HTTP returns a browser challenge or no readable text, renders JavaScript in a fresh browser "
        "through the configured proxy. Returns text, read_method and route diagnostics without proxy credentials. "
        "Does not use personal browser profiles, log in, solve CAPTCHA or claim video speech was transcribed.")
    reader["description"] += (
        " A login-only screen or playback error is unavailable content, not a successfully read article or transcript. "
        "Use content_kind=page_text literally: it does not establish what was said in a video.")
    windows = next(item for item in result if item["name"] == "windows_command")
    windows["description"] = (
        "Execute a command on the user's real Windows PC after approval. PowerShell by default, or cmd. "
        "Returns actual stdout and exit code. For user-requested URL reading use fetch_webpage first: "
        "This is a hidden noninteractive command; all child processes are killed on completion. "
        "To open a visible persistent terminal use windows_open_app, never Start-Process here. "
        "shell downloads do not have its automatic proxy fallback. "
        "Network commands are available for explicitly requested shell/network development or diagnostics.")
    windows["inputSchema"] = {"type": "object", "properties": {"command": {"type": "string"},
        "shell": {"type": "string", "enum": ["powershell", "cmd"]}, "cwd": {"type": "string"}},
        "required": ["command"], "additionalProperties": False}
    writer = next(item for item in result if item["name"] == "windows_write_file")
    writer["description"] = (
        "Create a new UTF-8 text/source-code file (including .py, .js, .ts, .html, .ps1), "
        "text document or DOCX on the user's real Windows Desktop or selected project, after kid's approval. "
        "This host tool has write access independently of the built-in command sandbox. "
        "Never overwrites or executes the file. Returns the real Windows path. "
        "For changes to an existing file use windows_command.")
    return [item for item in result if search == "kid" or item["name"] not in NETWORK_TOOLS]
