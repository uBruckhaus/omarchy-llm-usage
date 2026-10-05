#!/usr/bin/env python3
"""Stream local-LLM usage as one JSON line per interval (stdlib only).

Runtimes are identified by executable path and systemd cgroup, not by process
name: LM Studio's engine is also called `llama-server`.

- llama.cpp router  llama-server.service (or any other llama-server), plus the
                    one-shot llama.cpp tools (llama-tts, llama-cli, ...) that load a
                    model for a single run, e.g. Simple Reader's Qwen voices
- Ollama            ollama serve / ollama runner
- LM Studio         the lm-studio daemon and its bundled engines (~/.lmstudio)
- vLLM              `vllm serve` / vllm.entrypoints and its VLLM::* engine processes

Per-process GPU memory comes from DRM fdinfo (drm-memory-vram), deduplicated by
drm-client-id, so the monitor shows which runtime actually holds VRAM.
Model details come from the runtimes' local HTTP APIs, which are only queried
while the runtime is running (no API call ever starts a service).
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

INTERVAL = float(os.environ.get("LLM_USAGE_INTERVAL", "2"))
PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")
CPUS = os.cpu_count() or 1
PORTS = {"llamacpp": 8080, "ollama": 11434, "lmstudio": 1234, "vllm": 8000}  # vllm: --port wins
NAMES = {"llamacpp": "llama.cpp", "ollama": "Ollama", "lmstudio": "LM Studio", "vllm": "vLLM"}
RUNTIMES = ("llamacpp", "ollama", "lmstudio", "vllm")


def read(path, default=""):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return default


def http_json(port, path, timeout=0.6):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


# --- GPU and system -----------------------------------------------------------

def discrete_gpu():
    """The card with the largest VRAM (skips the iGPU carve-out)."""
    best = None
    for total in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            size = int(read(total, "0"))
        except ValueError:
            continue
        if not best or size > best[0]:
            best = (size, os.path.dirname(total))
    return best[1] if best else None


GPU_DIR = discrete_gpu()
GPU_SLOT = (re.search(r"PCI_SLOT_NAME=(\S+)", read(f"{GPU_DIR}/uevent")) or [None, ""])[1] if GPU_DIR else ""
HWMON = (glob.glob(f"{GPU_DIR}/hwmon/hwmon*") or [None])[0] if GPU_DIR else None


def gpu_stats():
    if not GPU_DIR:
        return None
    def number(path, scale=1.0):
        try:
            return float(read(path).strip()) / scale
        except ValueError:
            return None
    uevent = read(f"{GPU_DIR}/uevent")
    match = re.search(r"PCI_SLOT_NAME=(\S+)", uevent)
    return {
        "vram_used": int(number(f"{GPU_DIR}/mem_info_vram_used") or 0),
        "vram_total": int(number(f"{GPU_DIR}/mem_info_vram_total") or 0),
        "busy": number(f"{GPU_DIR}/gpu_busy_percent"),
        "temp": number(f"{HWMON}/temp1_input", 1000) if HWMON else None,
        "power": number(f"{HWMON}/power1_average", 1e6) if HWMON else None,
        "slot": match.group(1) if match else "",
    }


_last_cpu = None


def system_stats():
    global _last_cpu
    fields = [int(value) for value in read("/proc/stat").splitlines()[0].split()[1:]]
    idle, total = fields[3] + fields[4], sum(fields[:8])
    cpu = None
    if _last_cpu:
        busy = (total - _last_cpu[1]) - (idle - _last_cpu[0])
        cpu = round(100.0 * busy / max(1, total - _last_cpu[1]), 1)
    _last_cpu = (idle, total)
    info = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    return {"cpu": cpu, "mem_used": info.get("MemTotal", 0) - info.get("MemAvailable", 0), "mem_total": info.get("MemTotal", 0)}


# --- Processes ----------------------------------------------------------------

def clean(text, limit=120):
    """Names from runtime APIs and /proc, safe to show in any Qt label.

    Without "<" and ">" Qt never treats the text as rich text, so a model
    or process name cannot carry markup such as remote images.
    """
    text = "".join(ch for ch in str(text or "") if ch.isprintable() and ch not in "<>&")
    return text.strip()[:limit]


def trusted(pid):
    """Our own processes, or system services, which only root can start.

    Another local account's processes would let that account choose the
    names, model names and API answers this widget shows.
    """
    try:
        if os.stat(f"/proc/{pid}").st_uid == os.getuid():
            return True
    except OSError:
        return False
    return any(line.split(":", 2)[-1].startswith("/system.slice/")
               for line in read(f"/proc/{pid}/cgroup").splitlines())


# One-shot llama.cpp programs: they load a model on the GPU for one run and serve
# no HTTP API, so they report VRAM, CPU and RAM but no context or output buffers.
# (/proc/<pid>/comm is cut to 15 characters.)
LLAMACPP_TOOLS = ("llama-tts", "llama-cli", "llama-mtmd-cli", "llama-run", "llama-simple")


def classify(pid, comm, cmdline):
    # Several runtimes ship an engine called llama-server (Ollama 0.3x, LM Studio),
    # so the owning systemd unit and the executable path decide, not the name.
    cgroup = read(f"/proc/{pid}/cgroup")
    if "ollama.service" in cgroup or comm == "ollama" or "/ollama/" in cmdline or "/blobs/sha256-" in cmdline:
        return "ollama"
    if ".lmstudio" in cmdline or "lm-studio" in cmdline or comm.startswith("lm-studio"):
        return "lmstudio"
    if "llama-server.service" in cgroup or comm == "llama-server" or comm in LLAMACPP_TOOLS:
        return "llamacpp"
    # vLLM renames its engine and worker processes to VLLM::EngineCore, VLLM::Worker...
    if comm.lower().startswith("vllm") or re.search(r"\bvllm(\s+serve\b|\.entrypoints)", cmdline):
        return "vllm"
    return None


def process_table():
    """Runtime processes, grouped by runtime id."""
    groups = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = entry.name
        comm = read(f"/proc/{pid}/comm").strip()
        maybe_vllm = comm.lower().startswith("vllm") or comm.startswith("python")
        if (comm not in ("llama-server", "ollama") + LLAMACPP_TOOLS
                and not comm.startswith("lm-studio") and not maybe_vllm):
            continue
        if not trusted(pid):
            continue
        cmdline = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        if comm.startswith("python") and "vllm" not in cmdline:
            continue
        runtime = classify(pid, comm, cmdline)
        if runtime:
            groups.setdefault(runtime, []).append((pid, comm, cmdline))
    return groups


def proc_ticks(pid):
    stat = read(f"/proc/{pid}/stat")
    fields = stat[stat.rfind(")") + 2:].split()
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return 0


def proc_pss(pid):
    for line in read(f"/proc/{pid}/smaps_rollup").splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    for line in read(f"/proc/{pid}/status").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _kib(info, *keys):
    for key in keys:
        match = re.search(rf"^{key}:\s*(\d+)\s*KiB", info, re.M)
        if match:
            return int(match.group(1)) * 1024
    return 0


def proc_vram(pid):
    """(resident, requested) GPU memory of a process, from DRM fdinfo, one entry per DRM client.

    `drm-memory-vram` / `amd-requested-vram` include buffers the driver evicted to
    system RAM when VRAM ran full; only `drm-resident-vram` is really on the GPU.
    """
    clients = {}
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return 0, 0
    for fd in fds:
        try:
            if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                continue
        except OSError:
            continue
        info = read(f"/proc/{pid}/fdinfo/{fd}")
        client = re.search(r"drm-client-id:\s*(\d+)", info)
        if not client or (GPU_SLOT and f"drm-pdev:\t{GPU_SLOT}" not in info and "drm-pdev:" in info):
            continue  # other GPU (e.g. the iGPU) or no DRM client
        resident = _kib(info, "drm-resident-vram", "drm-memory-vram")
        requested = max(resident, _kib(info, "amd-requested-vram", "drm-total-vram", "drm-memory-vram"))
        clients[client.group(1)] = (resident, requested)
    return sum(value[0] for value in clients.values()), sum(value[1] for value in clients.values())


_last_ticks = {}
_last_time = None


def model_from_cmdline(cmdline):
    match = re.search(r"(?:--model|-m)\s+(\S+)", cmdline)
    if not match:
        return ""
    name = os.path.basename(match.group(1))
    return clean(re.sub(r"\.gguf$", "", name))


# --- Clients: who is talking to a runtime right now ----------------------------

def socket_owners():
    owners = {}
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = fd_dir.split("/")[2]
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target.startswith("socket:["):
                    owners[target[8:-1]] = pid
        except OSError:
            continue
    return owners


def client_label(pid):
    cmdline = read(f"/proc/{pid}/cmdline").replace("\0", " ")
    comm = read(f"/proc/{pid}/comm").strip()
    if "app.native" in cmdline or "simple-recipes" in cmdline:
        return "Simple Recipes"
    if "/pi/" in cmdline or comm == "pi":
        return "pi"
    if "opencode" in cmdline:
        return "opencode"
    if "kindle-reader" in cmdline or "KindleReader" in cmdline or "simple-reader" in cmdline:
        return "Simple Reader"
    if "lm-studio" in cmdline:
        return None
    return clean(comm, 40) or None


def clients_by_port():
    wanted = {port: runtime for runtime, port in PORTS.items()}
    inodes = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in read(table).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "01":  # ESTABLISHED
                continue
            remote_port = int(parts[2].rsplit(":", 1)[1], 16)
            if remote_port in wanted:
                inodes[parts[9]] = wanted[remote_port]
    if not inodes:
        return {}
    owners = socket_owners()
    found = {}
    for inode, runtime in inodes.items():
        pid = owners.get(inode)
        label = client_label(pid) if pid and trusted(pid) else None
        if label and label not in found.setdefault(runtime, []):
            found[runtime].append(label)
    return found


# --- Runtime model details -------------------------------------------------------

# Flags that decide how much memory the context (KV cache, recurrent state) takes.
CONTEXT_FLAGS = {
    "-m": "-m", "--model": "-m", "-c": "-c", "--ctx-size": "-c",
    "-ctk": "-ctk", "--cache-type-k": "-ctk", "-ctv": "-ctv", "--cache-type-v": "-ctv",
    "-fa": "-fa", "--flash-attn": "-fa", "-np": "-np", "--parallel": "-np",
    "-ub": "-ub", "--ubatch-size": "-ub", "-b": "-b", "--batch-size": "-b",
}
CONTEXT_SWITCHES = {"-kvu", "--kv-unified", "-no-kvu", "--no-kv-unified", "--swa-full"}
_context_memory = {}


def context_memory(args):
    """Bytes llama.cpp allocates for the context of a model started with `args`.

    llama-fit-params (next to the llama-server binary) projects the memory
    breakdown without loading weights; `-dev none` keeps it off the GPU. The
    KV cache is allocated in full at load, so this is what the context holds
    in VRAM. Cached per argument set: it runs once per model load.
    """
    if not args:
        return None
    wanted, index = [], 1
    while index < len(args):
        flag = args[index]
        if flag in CONTEXT_FLAGS and index + 1 < len(args):
            wanted += [CONTEXT_FLAGS[flag], args[index + 1]]
            index += 2
            continue
        if flag in CONTEXT_SWITCHES:
            wanted.append(flag)
        index += 1
    if "-m" not in wanted:
        return None
    key = tuple(wanted)
    if key not in _context_memory:
        tool = os.path.join(os.path.dirname(args[0]), "llama-fit-params")
        total = None
        try:
            result = subprocess.run([tool, *wanted, "-dev", "none", "-lv", "4"],
                                    capture_output=True, text=True, timeout=30)
            # "|   - Host   |   7444 =  4935 +  1877 +  631   |": model + context + compute
            rows = re.findall(r"\|\s+- .*?\|\s+\d+\s*=\s*\d+\s*\+\s*(\d+)\s*\+\s*\d+", result.stderr + result.stdout)
            if rows:
                total = sum(int(row) for row in rows) * 2**20
        except (OSError, subprocess.SubprocessError):
            pass
        _context_memory[key] = total
    return _context_memory[key]


_last_output = {}
_speed = {}


def speed_state(key):
    """Generation-speed bookkeeping per runtime and model, kept while the collector runs."""
    return _speed.setdefault(key, {"rates": [], "tokens": 0.0, "seconds": 0.0,
                                   "counters": None, "live": None, "now": None, "server_avg": None})


def speed_add(state, tokens, seconds):
    if tokens > 0 and seconds > 0:
        state["rates"] = (state["rates"] + [tokens / seconds])[-500:]
        state["tokens"] += tokens
        state["seconds"] += seconds


def speed_report(state):
    """now / min / avg / max tokens per second; avg is weighted by tokens.

    The average prefers the runtime's own totals since the model was loaded, so
    it is there right after the monitor starts; min and max need finished replies.
    """
    rates = state["rates"]
    avg = state["server_avg"] or (state["tokens"] / state["seconds"] if state["seconds"] else None)
    if not rates and state["now"] is None and avg is None:
        return None
    return {"now": state["now"], "count": len(rates), "avg": avg,
            "min": min(rates) if rates else None, "max": max(rates) if rates else None}


def llamacpp_speed(model, state):
    """Speed of finished replies from llama.cpp's /metrics counters.

    tokens_predicted_total / tokens_predicted_seconds_total grow when a reply
    ends and count decode time only (no prompt processing), so each increase is
    the generation speed of the replies that just finished. Needs --metrics.
    """
    path = "/metrics" + (f"?model={urllib.parse.quote(model)}" if model else "")
    metrics = prometheus(PORTS["llamacpp"], path, "llamacpp:")
    tokens = sum(value for _, value in metrics.get("llamacpp:tokens_predicted_total", []))
    seconds = sum(value for _, value in metrics.get("llamacpp:tokens_predicted_seconds_total", []))
    if not metrics:
        return
    previous = state["counters"]
    if previous and tokens >= previous[0]:
        speed_add(state, tokens - previous[0], seconds - previous[1])
    elif previous:  # counters went back: the model was reloaded
        state.update(rates=[], tokens=0.0, seconds=0.0)
    state["counters"] = (tokens, seconds)
    state["server_avg"] = tokens / seconds if tokens > 0 and seconds > 0 else None


def llamacpp_slots(model=None):
    """Context and output fill, summed over the server's slots.

    context_used: tokens in the slots (n_prompt_tokens counts generated ones too).
    output_used / output_limit: tokens generated for the reply in progress and
    the most it may produce (n_predict, or what is left of the context). The
    last reply's figures stay visible after it ends, marked as not generating.
    """
    path = "/slots" + (f"?model={urllib.parse.quote(model)}" if model else "")
    slots = http_json(PORTS["llamacpp"], path)
    slots = [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else []
    if not slots:
        return {}
    used = sum(int(slot.get("n_prompt_tokens") or 0) for slot in slots)
    size = sum(int(slot.get("n_ctx") or 0) for slot in slots)
    output = {"output_used": 0, "output_limit": 0, "generating": False}
    tasks = tuple(sorted(slot.get("id_task") for slot in slots if slot.get("is_processing")))
    for slot in slots:
        if not slot.get("is_processing"):
            continue
        decoded = sum(int(token.get("n_decoded") or 0) for token in slot.get("next_token") or [] if isinstance(token, dict))
        limit = int((slot.get("params") or {}).get("n_predict") or -1)
        if limit <= 0:  # unlimited: the reply can grow until the context is full
            limit = max(decoded, int(slot.get("n_ctx") or 0) - int(slot.get("n_prompt_tokens") or 0) + decoded)
        output["output_used"] += decoded
        output["output_limit"] += limit
        output["generating"] = True
    key = model or ""
    state = speed_state(("llamacpp", key))
    now, live = time.monotonic(), state["live"]
    state["now"] = None
    if output["generating"]:
        if live and live[0] == tasks and output["output_used"] > live[1]:
            state["now"] = (output["output_used"] - live[1]) / max(0.001, now - live[2])
        elif live and live[0] == tasks:
            state["now"] = live[3]  # no new token since the last look: keep the last value
        state["live"] = (tasks, output["output_used"], now, state["now"])
    else:
        state["live"] = None
    llamacpp_speed(model, state)
    if output["generating"]:
        _last_output[key] = output
    else:
        output = dict(_last_output.get(key) or output, generating=False)
    return {"context_used": used, "context": size or None, **output, "speed": speed_report(state)}


def tool_clients(processes):
    """The programs that started one-shot llama.cpp tools (they have no socket to trace)."""
    found = []
    for pid, comm, _ in processes:
        if comm not in LLAMACPP_TOOLS:
            continue
        stat = read(f"/proc/{pid}/stat")
        try:
            parent = stat[stat.rfind(")") + 2:].split()[1]
        except IndexError:
            continue
        label = client_label(parent) if trusted(parent) else None
        if label and label not in found:
            found.append(label)
    return found


def llamacpp_models(processes):
    tools = [(pid, cmdline) for pid, comm, cmdline in processes if comm in LLAMACPP_TOOLS]
    processes = [process for process in processes if process[1] not in LLAMACPP_TOOLS]
    models = [] if not processes else llamacpp_server_models(processes)
    for _, cmdline in tools:
        name = model_from_cmdline(cmdline)
        if name and all(model["name"] != name for model in models):
            models.append({"name": name, "state": "loaded", "context": None})
    return models


def llamacpp_server_models(processes):
    models = []
    data = http_json(PORTS["llamacpp"], "/models")
    for row in (data or {}).get("data", []):
        state = (row.get("status") or {}).get("value", "")
        if state in ("loaded", "loading", "sleeping"):
            ctx = None
            args = (row.get("status") or {}).get("args") or []
            if "--ctx-size" in args:
                try:
                    ctx = int(args[args.index("--ctx-size") + 1])
                except (ValueError, IndexError):
                    pass
            model = {"name": clean(row.get("id")), "state": state, "context": ctx}
            if state == "loaded":
                slots = llamacpp_slots(row.get("id"))
                model.update(slots, context=slots.get("context") or ctx, context_memory=context_memory(args))
            models.append(model)
    if not models:
        for pid, _, cmdline in processes:
            name = model_from_cmdline(cmdline)
            if name:
                args = read(f"/proc/{pid}/cmdline").split("\0")
                models.append({"name": name, "state": "loaded", "context": None, **llamacpp_slots(),
                               "context_memory": context_memory([arg for arg in args if arg])})
    return models


def ollama_models():
    data = http_json(PORTS["ollama"], "/api/ps")
    return [{"name": clean(str(row.get("name") or "").removesuffix(":latest")), "state": "loaded",
             "context": row.get("context_length"), "vram": row.get("size_vram")}
            for row in (data or {}).get("models", [])]


def lmstudio_models(processes):
    models = []
    data = http_json(PORTS["lmstudio"], "/api/v1/models") if port_open(PORTS["lmstudio"]) else None
    for row in (data or {}).get("models", []):
        for instance in row.get("loaded_instances", []):
            config = instance.get("config") or {}
            models.append({"name": clean(row.get("key")), "state": "loaded", "context": config.get("context_length")})
    if not models:
        # The daemon may run without its HTTP server; the engine's cmdline still names the model.
        for _, comm, cmdline in processes:
            name = model_from_cmdline(cmdline) if comm == "llama-server" else ""
            if name:
                models.append({"name": name, "state": "loaded", "context": None})
    return models


def vllm_port(processes):
    """The API port from the `vllm serve` command line (default 8000)."""
    for _, _, cmdline in processes:
        match = re.search(r"--port[=\s]+(\d+)", cmdline)
        if match:
            return int(match.group(1))
    return 8000


def prometheus(port, path="/metrics", prefix="vllm:"):
    """Prometheus samples from a runtime's metrics endpoint as {name: [(labels, value)]}."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=0.6) as response:
            text = response.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return {}
    samples = {}
    for line in text.splitlines():
        match = re.match(rf"^({re.escape(prefix)}[A-Za-z0-9_:]+)(\{{[^}}]*\}})?\s+(\S+)", line)
        if not match:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', match.group(2) or ""))
        try:
            samples.setdefault(match.group(1), []).append((labels, float(match.group(3))))
        except ValueError:
            pass
    return samples


def vllm_models(processes):
    """Model, KV-cache fill and activity of a vLLM server.

    vLLM keeps one KV-cache pool for all requests (num_gpu_blocks x block_size
    tokens), reserved at start-up; kv_cache_usage_perc (gpu_cache_usage_perc
    before v0.10) is how full that pool is, which is what "Context buffer" shows.
    """
    port = PORTS["vllm"]
    data = http_json(port, "/v1/models")
    metrics = prometheus(port)
    usage = (metrics.get("vllm:kv_cache_usage_perc") or metrics.get("vllm:gpu_cache_usage_perc") or [({}, None)])[0][1]
    capacity = None
    for labels, _ in metrics.get("vllm:cache_config_info", []):
        try:
            capacity = int(labels["num_gpu_blocks"]) * int(labels["block_size"])
        except (KeyError, ValueError):
            pass
    running = sum(value for _, value in metrics.get("vllm:num_requests_running", []))
    generated = sum(value for _, value in metrics.get("vllm:generation_tokens_total", []))
    state, now = speed_state(("vllm", port)), time.monotonic()
    previous, state["now"] = state["counters"], None
    if previous and generated >= previous[0] and running > 0:
        state["now"] = (generated - previous[0]) / max(0.001, now - previous[1])
        speed_add(state, generated - previous[0], now - previous[1])
    elif previous and generated < previous[0]:  # server restarted
        state.update(rates=[], tokens=0.0, seconds=0.0)
    if metrics:
        state["counters"] = (generated, now)
    models = []
    for row in (data or {}).get("data", []):
        if not isinstance(row, dict) or row.get("parent"):  # skip LoRA adapters
            continue
        model = {"name": clean(row.get("id")), "state": "loaded", "context": row.get("max_model_len"),
                 "generating": running > 0, "speed": speed_report(state)}
        if capacity and usage is not None:
            model.update(context=capacity, context_used=round(usage * capacity))
        models.append(model)
    return models


UNIT_DIRS = ("~/.config/systemd/user", "/etc/systemd/user", "/usr/lib/systemd/user",
             "/etc/systemd/system", "/usr/lib/systemd/system")
INSTALL_HINTS = {
    "llamacpp": {"commands": ("llama-server",), "units": ("llama-server.service",)},
    "ollama": {"commands": ("ollama",), "units": ("ollama.service",)},
    "lmstudio": {"commands": ("lms", "lm-studio"), "paths": ("~/.lmstudio",)},
    "vllm": {"commands": ("vllm",), "units": ("vllm.service",), "module": "vllm"},
}
_installed = {"tick": 0, "found": {}}


def installed_runtimes(every=30):
    """Which runtimes exist on this machine, refreshed every `every` snapshots (about a minute).

    Lets the panel say "not installed" instead of "stopped". A runtime counts as
    installed when its command is on PATH, a systemd unit for it exists, its data
    folder exists, or (vLLM) the Python package is importable.
    """
    _installed["tick"] += 1
    if _installed["tick"] % every != 1:
        return _installed["found"]
    found = {}
    for runtime, hints in INSTALL_HINTS.items():
        hit = any(shutil.which(command) for command in hints.get("commands", ()))
        hit = hit or any(os.path.exists(os.path.expanduser(f"{folder}/{unit}"))
                         for unit in hints.get("units", ()) for folder in UNIT_DIRS)
        hit = hit or any(os.path.exists(os.path.expanduser(path)) for path in hints.get("paths", ()))
        if not hit and hints.get("module"):
            try:
                hit = importlib.util.find_spec(hints["module"]) is not None
            except (ImportError, ValueError):
                hit = False
        found[runtime] = hit
    _installed["found"] = found
    return found


_other_cache = {"tick": 0, "apps": []}


def other_gpu_apps(llm_pids, every=5):
    """Non-LLM programs holding VRAM (desktop, browser, ...), refreshed every `every` snapshots."""
    _other_cache["tick"] += 1
    if _other_cache["tick"] % every != 1:
        return _other_cache["apps"]
    usage = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or entry.name in llm_pids or not trusted(entry.name):
            continue
        resident, _ = proc_vram(entry.name)
        if resident >= 16 * 2**20:
            name = clean(read(f"/proc/{entry.name}/comm"), 40) or entry.name
            usage[name] = usage.get(name, 0) + resident
    _other_cache["apps"] = [{"name": name, "vram": vram} for name, vram in sorted(usage.items(), key=lambda item: -item[1])[:5]]
    return _other_cache["apps"]


def primary(runtime):
    model = next((model for model in runtime["models"] if model["state"] != "sleeping"), {})
    keys = ("context", "context_used", "context_memory", "output_used", "output_limit", "generating", "speed")
    return {"runtime": runtime["name"], "model": model.get("name", ""), **{key: model.get(key) for key in keys}}


def snapshot():
    global _last_time
    now = time.monotonic()
    elapsed = (now - _last_time) if _last_time else None
    _last_time = now
    groups = process_table()
    if groups.get("vllm"):
        PORTS["vllm"] = vllm_port(groups["vllm"])
    clients = clients_by_port() if groups else {}
    for label in tool_clients(groups.get("llamacpp", [])):
        if label not in clients.setdefault("llamacpp", []):
            clients["llamacpp"].append(label)
    runtimes, seen = [], set()
    installed = installed_runtimes()
    for runtime in RUNTIMES:
        processes = groups.get(runtime, [])
        ticks = cpu = 0.0
        pss = vram = requested = 0
        for pid, _, _ in processes:
            seen.add(pid)
            current = proc_ticks(pid)
            if elapsed and pid in _last_ticks:
                ticks += max(0, current - _last_ticks[pid])
            _last_ticks[pid] = current
            pss += proc_pss(pid)
            resident, wanted = proc_vram(pid)
            vram += resident
            requested += wanted
        if elapsed:
            cpu = round(100.0 * ticks / TICKS / elapsed / CPUS, 1)
        if not processes:
            models = []
        elif runtime == "llamacpp":
            models = llamacpp_models(processes)
        elif runtime == "ollama":
            models = ollama_models()
        elif runtime == "vllm":
            models = vllm_models(processes)
        else:
            models = lmstudio_models(processes)
        runtimes.append({"id": runtime, "name": NAMES[runtime], "running": bool(processes),
                         "installed": bool(processes) or installed.get(runtime, True),
                         "processes": len(processes), "cpu": cpu if elapsed else None,
                         "memory": pss, "vram": vram, "models": models,
                         # Asked for VRAM but evicted to system RAM: the GPU is overcommitted.
                         "spilled": max(0, requested - vram),
                         "clients": clients.get(runtime, [])})
    for pid in list(_last_ticks):
        if pid not in seen:
            del _last_ticks[pid]
    gpu = gpu_stats()
    other_apps = other_gpu_apps(seen)
    # "sleeping" (llama.cpp router idle-unload) keeps the model listed but frees the GPU.
    loaded = [runtime for runtime in runtimes
              if any(model["state"] in ("loaded", "loading") for model in runtime["models"]) or runtime["vram"] > 512 * 2**20]
    llm_vram = sum(runtime["vram"] for runtime in runtimes)
    if gpu and gpu.get("vram_used"):
        llm_vram = min(llm_vram, gpu["vram_used"])  # never claim more than the GPU reports
    return {
        "time": int(time.time()),
        "gpu": gpu,
        "system": system_stats(),
        "runtimes": runtimes,
        "active": bool(loaded),
        "llm_vram": llm_vram,
        "llm_spilled": sum(runtime["spilled"] for runtime in runtimes),
        # Everything else on the GPU: desktop compositor, shell, browser, driver.
        "other_vram": max(0, (gpu or {}).get("vram_used", 0) - llm_vram),
        "other_apps": other_apps,
        # Generating: a model is resident and the GPU is working.
        "busy": bool(loaded) and bool(gpu and (gpu.get("busy") or 0) >= 20),
        "primary": next((primary(runtime) for runtime in sorted(loaded, key=lambda r: -r["vram"])), None),
    }


def main():
    once = "--once" in sys.argv
    snapshot()  # prime CPU counters
    time.sleep(0.5 if once else min(INTERVAL, 1))
    while True:
        try:
            print(json.dumps(snapshot()), flush=True)
        except BrokenPipeError:
            return 0
        except Exception as exc:  # keep streaming; the bar shows stale data otherwise
            print(json.dumps({"error": str(exc)[:200]}), flush=True)
        if once:
            return 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
