#!/usr/bin/env bash
# Free all GPU memory held by local LLM runtimes. Stopping a systemd unit ends
# its model workers (KillMode=control-group); LM Studio keeps its daemon but
# unloads every model. The notification says what was stopped, what failed and
# which units could not be checked.
set -u
freed=() failed=() unknown=()
for unit in llama-server ollama vllm; do
  # A unit that is running or starting is stopped. is-active exits 3 for any
  # other state (inactive, failed, ...) and 4 for no such unit; anything else
  # (e.g. no user bus) means the state is unknown: reported, not taken as "off".
  state="$(timeout 10 systemctl --user is-active "$unit.service" 2>/dev/null)"
  rc=$?
  case "$state" in
    active|activating|reloading|refreshing) ;;
    *) case $rc in
         3|4) continue ;;
         *) unknown+=("$unit"); continue ;;
       esac ;;
  esac
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
if ((${#failed[@]} + ${#unknown[@]})); then
  title="GPU memory not fully freed" body=""
  ((${#failed[@]})) && body+="Could not stop: $(list "${failed[@]}"). "
  ((${#unknown[@]})) && body+="Could not check: $(list "${unknown[@]}") (systemctl --user did not answer). "
  ((${#freed[@]})) && body+="Stopped: $(list "${freed[@]}")."
  body="${body% }"
elif ((${#freed[@]})); then
  title="GPU memory freed" body="Stopped: $(list "${freed[@]}")."
else
  title="Nothing to free" body="No llama-server, Ollama or vLLM user service and no LM Studio models are running. Servers started by hand must be stopped where they run."
fi
notify-send -a "LLM Monitor" "$title" "$body" 2>/dev/null || true
((${#failed[@]} + ${#unknown[@]} == 0))
