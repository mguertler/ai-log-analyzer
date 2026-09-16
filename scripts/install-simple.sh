#!/bin/sh
set -eu

printf '%s\n' "INFO: To install this script as python package use 'make install-pipx'; requires pipx on your system."
printf '%s\n\n' "INFO: Continuing with simple standalone script installation."

SCRIPT_SOURCE="src/ai_log_analyzer"
CONFIG_EXAMPLE="ai-log-analyzer.conf.example"
CONFIG_ONLY=0
if [ "${1:-}" = "--config-only" ]; then
  CONFIG_ONLY=1
fi

if [ "$CONFIG_ONLY" -eq 0 ] && [ ! -f "$SCRIPT_SOURCE" ]; then
  echo "Error: $SCRIPT_SOURCE not found. Run this from the repository root." >&2
  exit 1
fi
if [ ! -f "$CONFIG_EXAMPLE" ]; then
  echo "Error: $CONFIG_EXAMPLE not found. Run this from the repository root." >&2
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  DEFAULT_SCRIPT_PATH="/usr/local/bin/ai-log-analyzer"
  DEFAULT_CONFIG_PATH="/usr/local/etc/ai-log-analyzer.conf"
else
  DEFAULT_SCRIPT_PATH="$HOME/.local/bin/ai-log-analyzer"
  XDG_CONFIG_HOME_VALUE="${XDG_CONFIG_HOME:-$HOME/.config}"
  DEFAULT_CONFIG_PATH="$XDG_CONFIG_HOME_VALUE/ai-log-analyzer/ai-log-analyzer.conf"
fi

ask() {
  prompt="$1"
  default="$2"
  printf "%s [%s]: " "$prompt" "$default" >&2
  IFS= read -r answer || answer=""
  if [ -z "$answer" ]; then
    printf '%s\n' "$default"
  else
    printf '%s\n' "$answer"
  fi
}

ask_secret() {
  ask "$1" "$2"
}

ask_yes_no() {
  prompt="$1"
  default="$2"
  printf "%s [%s]: " "$prompt" "$default" >&2
  IFS= read -r answer || answer=""
  if [ -z "$answer" ]; then
    answer="$default"
  fi
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$CONFIG_ONLY" -eq 0 ]; then
  SCRIPT_PATH=$(ask "Where should the script be installed?" "$DEFAULT_SCRIPT_PATH")
else
  SCRIPT_PATH="ai-log-analyzer"
fi
CONFIG_PATH=$(ask "Where should the config file be installed?" "$DEFAULT_CONFIG_PATH")

CONFIG_DIR=$(dirname "$CONFIG_PATH")
mkdir -p "$CONFIG_DIR"
if [ "$CONFIG_ONLY" -eq 0 ]; then
  SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
  mkdir -p "$SCRIPT_DIR"
  cp "$SCRIPT_SOURCE" "$SCRIPT_PATH"
  chmod 0755 "$SCRIPT_PATH"
fi

merge_config() {
  old_config="$1"
  example_config="$2"
  out_config="$3"
  export old_config example_config out_config
  python3 - <<'PY'
import re
import os
from pathlib import Path

old_path = Path(os.environ["old_config"])
example_path = Path(os.environ["example_config"])
out_path = Path(os.environ["out_config"])

KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")
HEREDOC_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*<<\s*(\S+)\s*$")

LEGACY_KEY_MAP = {}

def remap_line_key(line: str, old_key: str, new_key: str) -> str:
    return re.sub(r"^(\s*)" + re.escape(old_key) + r"(\s*=)", r"\1" + new_key + r"\2", line, count=1)

def read_values(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            i += 1
            continue
        m = HEREDOC_RE.match(line)
        if m:
            key, marker = m.group(1), m.group(2)
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i].strip() == marker:
                    i += 1
                    break
                i += 1
            new_key = LEGACY_KEY_MAP.get(key, key)
            if new_key != key:
                block[0] = re.sub(r"^(\s*)" + re.escape(key) + r"(\s*<<)", r"\1" + new_key + r"\2", block[0], count=1)
            values[new_key] = block
            continue
        m = KEY_RE.match(line)
        if m:
            key = m.group(1)
            new_key = LEGACY_KEY_MAP.get(key, key)
            if new_key != key:
                line = remap_line_key(line, key, new_key)
            values[new_key] = [line]
        i += 1
    return values

old_values = read_values(old_path)
example_lines = example_path.read_text(encoding="utf-8").splitlines()
out = []
i = 0
used = set()
while i < len(example_lines):
    line = example_lines[i]
    m = HEREDOC_RE.match(line)
    if m:
        key, marker = m.group(1), m.group(2)
        if key in old_values:
            out.extend(old_values[key])
            used.add(key)
            i += 1
            while i < len(example_lines):
                if example_lines[i].strip() == marker:
                    i += 1
                    break
                i += 1
            continue
        out.append(line)
        i += 1
        while i < len(example_lines):
            out.append(example_lines[i])
            if example_lines[i].strip() == marker:
                i += 1
                break
            i += 1
        continue
    m = KEY_RE.match(line)
    if m and m.group(1) in old_values:
        out.extend(old_values[m.group(1)])
        used.add(m.group(1))
    else:
        out.append(line)
    i += 1

extra_keys = [key for key in old_values if key not in used]
if extra_keys:
    out.append("")
    out.append("# Preserved custom parameters from previous config")
    for key in extra_keys:
        out.extend(old_values[key])

out_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
}

if [ -e "$CONFIG_PATH" ]; then
  echo "Config already exists: $CONFIG_PATH"
  BACKUP_PATH="$CONFIG_PATH.backup.$(date +%Y%m%d-%H%M%S)"
  cp "$CONFIG_PATH" "$BACKUP_PATH"
  echo "Backup created: $BACKUP_PATH"
  if ask_yes_no "Merge existing values with the new example config?" "Y"; then
    TMP_CONFIG="$CONFIG_PATH.tmp.$$"
    merge_config "$CONFIG_PATH" "$CONFIG_EXAMPLE" "$TMP_CONFIG"
    mv "$TMP_CONFIG" "$CONFIG_PATH"
    echo "Merged existing values into new config template."
  elif ask_yes_no "Overwrite existing config with the new example config?" "n"; then
    cp "$CONFIG_EXAMPLE" "$CONFIG_PATH"
    echo "Existing config overwritten."
  else
    echo "Keeping existing config unchanged."
  fi
else
  cp "$CONFIG_EXAMPLE" "$CONFIG_PATH"
fi
chmod 0600 "$CONFIG_PATH" 2>/dev/null || true

if ask_yes_no "Edit configuration interactively?" "Y"; then
  echo ""
  echo "Chat Completions API is used by default for OpenAI, LiteLLM, and Ollama compatibility."
  echo "For Ollama's native API (per-call context size, thinking control) set openai.api_style = ollama in the config afterwards."
  echo "Examples:"
  echo "  OpenAI:  https://api.openai.com"
  echo "  LiteLLM: http://127.0.0.1:4000"
  echo "  Ollama:  http://127.0.0.1:11434"
  echo ""

  API_URL=$(ask "API base URL" "https://api.openai.com")
  API_KEY=$(ask_secret "API key (empty = use OPENAI_API_KEY environment variable)" "")
  MODEL=$(ask "Model" "gpt-5-mini")
  MAX_OUTPUT_TOKENS=$(ask "Maximum output tokens" "8192")
  echo ""
  echo "Chunk size tips:"
  echo "  200 lines  = safer for smaller/local models or very noisy logs"
  echo "  300 lines  = conservative local-model setting"
  echo "  500 lines  = recommended default; best for 64k context-size with thinking model"
  echo "  800 lines  = for larger/stable context windows"
  echo "  1000+ lines = may fail or return empty results despite nominal 64k context"
  CHUNK_SIZE=$(ask "Chunk size in log lines" "500")
  MAX_PARALLEL=$(ask "Maximum parallel chunk requests (1 = sequential)" "1")
  MAX_LINES=$(ask "Maximum filtered lines before abort" "15000")
  TAIL_LINES=$(ask "Default tail limit for input lines (0 = no limit)" "0")

  export API_KEY API_URL MODEL MAX_OUTPUT_TOKENS CHUNK_SIZE MAX_PARALLEL MAX_LINES TAIL_LINES CONFIG_PATH
  python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["CONFIG_PATH"])
updates = {
    "openai.api_url": os.environ.get("API_URL", "https://api.openai.com"),
    "openai.api_key": os.environ.get("API_KEY", ""),
    "openai.api_path": "/v1/chat/completions",
    "openai.api_style": "chat_completions",
    "openai.model": os.environ.get("MODEL", "gpt-5-mini"),
    "openai.max_output_tokens": os.environ.get("MAX_OUTPUT_TOKENS", "8192"),
    "logs.chunk_size": os.environ.get("CHUNK_SIZE", "500"),
    "logs.max_parallel": os.environ.get("MAX_PARALLEL", "1"),
    "logs.max_lines": os.environ.get("MAX_LINES", "15000"),
    "logs.tail_lines": os.environ.get("TAIL_LINES", "0"),
    "timestamps.enabled": "true",
}

def fmt(value: str) -> str:
    value = str(value)
    if value == "" or value.startswith((" ", "#", ";")) or value.endswith(" "):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return value

lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
out = []
in_heredoc = None
for line in lines:
    stripped = line.strip()
    if in_heredoc:
        out.append(line)
        if stripped == in_heredoc:
            in_heredoc = None
        continue
    if stripped and not stripped.startswith(("#", ";")) and "<<" in line and "=" not in line:
        key = line.split("<<", 1)[0].strip()
        in_heredoc = line.split("<<", 1)[1].strip()
        out.append(line)
        if key in updates:
            seen.add(key)
        continue
    replaced = False
    if stripped and not stripped.startswith(("#", ";")) and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key} = {fmt(updates[key])}")
            seen.add(key)
            replaced = True
    if not replaced:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key} = {fmt(value)}")
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
fi

echo ""
if [ "$CONFIG_ONLY" -eq 0 ]; then
  echo "Installed script: $SCRIPT_PATH"
else
  echo "Installed command: ai-log-analyzer"
fi
echo "Installed config: $CONFIG_PATH"
echo ""
echo "Example runs:"
echo "  journalctl --since \"24 hours ago\" -p \"warning..alert\" | ai-log-analyzer --config $CONFIG_PATH"
echo "  ai-log-analyzer --config $CONFIG_PATH /var/log/syslog"
echo ""
if [ "$CONFIG_ONLY" -eq 0 ]; then
  case ":$PATH:" in
    *":$(dirname "$SCRIPT_PATH"):"*) ;;
    *) echo "Note: $(dirname "$SCRIPT_PATH") is not in PATH. Use the full path above or add it to PATH." ;;
  esac
fi
