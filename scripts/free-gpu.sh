#!/usr/bin/env bash
# Free all GPU memory held by local LLM runtimes. Stopping a systemd unit ends
# its model workers (KillMode=control-group); LM Studio keeps its daemon but
# unloads every model.
set -u
systemctl --user stop llama-server.service 2>/dev/null
systemctl --user stop ollama.service 2>/dev/null
lms_bin="$(command -v lms || echo "$HOME/.lmstudio/bin/lms")"
if [[ -x "$lms_bin" ]] && pgrep -f "lm-studio --run-as-service|\.lmstudio/extensions" >/dev/null; then
  "$lms_bin" unload --all >/dev/null 2>&1
fi
notify-send -a "LLM Monitor" "GPU memory freed" "Local LLM models were unloaded." 2>/dev/null || true
