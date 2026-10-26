#!/usr/bin/env bash
# Free all GPU memory held by local LLM runtimes. Stopping a systemd unit ends
# its model workers (KillMode=control-group); LM Studio keeps its daemon but
# unloads every model. The notification says what was stopped and what failed.
set -u
freed=() failed=()
for unit in llama-server ollama vllm; do
  systemctl --user is-active --quiet "$unit.service" || continue
  if timeout 60 systemctl --user stop "$unit.service" 2>/dev/null; then
    freed+=("$unit")
  else
    failed+=("$unit")
  fi
done
lms_bin="$(command -v lms || echo "$HOME/.lmstudio/bin/lms")"
if [[ -x "$lms_bin" ]] && pgrep -f "lm-studio --run-as-service|\.lmstudio/extensions" >/dev/null; then
  if timeout 60 "$lms_bin" unload --all >/dev/null 2>&1; then freed+=("LM Studio models"); else failed+=("LM Studio"); fi
fi
list() { local IFS=,; echo "$*" | sed 's/,/, /g'; }
if ((${#failed[@]})); then
  title="GPU memory not fully freed" body="Could not stop: $(list "${failed[@]}")."
  ((${#freed[@]})) && body+=" Stopped: $(list "${freed[@]}")."
elif ((${#freed[@]})); then
  title="GPU memory freed" body="Stopped: $(list "${freed[@]}")."
else
  title="Nothing to free" body="No llama-server, Ollama or vLLM user service and no LM Studio models are running. Servers started by hand must be stopped where they run."
fi
notify-send -a "LLM Monitor" "$title" "$body" 2>/dev/null || true
((${#failed[@]} == 0))
