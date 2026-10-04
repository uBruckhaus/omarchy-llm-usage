#!/usr/bin/env python3
"""Stream local-LLM usage as one JSON line per interval (stdlib only).

Runtimes are identified by executable path and systemd cgroup, not by process
name: LM Studio's engine is also called `llama-server`.

- llama.cpp router  llama-server.service (or any other llama-server)
- Ollama            ollama serve / ollama runner
- LM Studio         the lm-studio daemon and its bundled engines (~/.lmstudio)

Per-process GPU memory comes from DRM fdinfo (drm-memory-vram), deduplicated by
drm-client-id, so the monitor shows which runtime actually holds VRAM.
Model details come from the runtimes' local HTTP APIs, which are only queried
while the runtime is running (no API call ever starts a service).
"""
import glob
import json
import os
import re
import socket
import sys
import time
import urllib.request

INTERVAL = float(os.environ.get("LLM_USAGE_INTERVAL", "2"))
PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")
CPUS = os.cpu_count() or 1
PORTS = {"llamacpp": 8080, "ollama": 11434, "lmstudio": 1234}
NAMES = {"llamacpp": "llama.cpp", "ollama": "Ollama", "lmstudio": "LM Studio"}


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

def classify(pid, comm, cmdline):
    # Several runtimes ship an engine called llama-server (Ollama 0.3x, LM Studio),
    # so the owning systemd unit and the executable path decide, not the name.
    cgroup = read(f"/proc/{pid}/cgroup")
    if "ollama.service" in cgroup or comm == "ollama" or "/ollama/" in cmdline or "/blobs/sha256-" in cmdline:
        return "ollama"
    if ".lmstudio" in cmdline or "lm-studio" in cmdline or comm.startswith("lm-studio"):
        return "lmstudio"
    if "llama-server.service" in cgroup or comm == "llama-server":
        return "llamacpp"
    return None


def process_table():
    """Runtime processes, grouped by runtime id."""
    groups = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = entry.name
        comm = read(f"/proc/{pid}/comm").strip()
        if comm not in ("llama-server", "ollama") and not comm.startswith("lm-studio"):
            continue
        cmdline = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
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
    return re.sub(r"\.gguf$", "", name)


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
    if "lm-studio" in cmdline:
        return None
    return comm or None


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
        label = client_label(pid) if pid else None
        if label and label not in found.setdefault(runtime, []):
            found[runtime].append(label)
    return found


# --- Runtime model details -------------------------------------------------------

def llamacpp_models(processes):
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
            models.append({"name": row.get("id", ""), "state": state, "context": ctx})
    if not models:
        for _, _, cmdline in processes:
            name = model_from_cmdline(cmdline)
            if name:
                models.append({"name": name, "state": "loaded", "context": None})
    return models


def ollama_models():
    data = http_json(PORTS["ollama"], "/api/ps")
    return [{"name": row.get("name", "").removesuffix(":latest"), "state": "loaded",
             "context": row.get("context_length"), "vram": row.get("size_vram")}
            for row in (data or {}).get("models", [])]


def lmstudio_models(processes):
    models = []
    data = http_json(PORTS["lmstudio"], "/api/v1/models") if port_open(PORTS["lmstudio"]) else None
    for row in (data or {}).get("models", []):
        for instance in row.get("loaded_instances", []):
            config = instance.get("config") or {}
            models.append({"name": row.get("key", ""), "state": "loaded", "context": config.get("context_length")})
    if not models:
        # The daemon may run without its HTTP server; the engine's cmdline still names the model.
        for _, comm, cmdline in processes:
            name = model_from_cmdline(cmdline) if comm == "llama-server" else ""
            if name:
                models.append({"name": name, "state": "loaded", "context": None})
    return models


_other_cache = {"tick": 0, "apps": []}


def other_gpu_apps(llm_pids, every=5):
    """Non-LLM programs holding VRAM (desktop, browser, ...), refreshed every `every` snapshots."""
    _other_cache["tick"] += 1
    if _other_cache["tick"] % every != 1:
        return _other_cache["apps"]
    usage = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or entry.name in llm_pids:
            continue
        resident, _ = proc_vram(entry.name)
        if resident >= 16 * 2**20:
            name = read(f"/proc/{entry.name}/comm").strip() or entry.name
            usage[name] = usage.get(name, 0) + resident
    _other_cache["apps"] = [{"name": name, "vram": vram} for name, vram in sorted(usage.items(), key=lambda item: -item[1])[:5]]
    return _other_cache["apps"]


def snapshot():
    global _last_time
    now = time.monotonic()
    elapsed = (now - _last_time) if _last_time else None
    _last_time = now
    groups = process_table()
    clients = clients_by_port() if groups else {}
    runtimes, seen = [], set()
    for runtime in ("llamacpp", "ollama", "lmstudio"):
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
        else:
            models = lmstudio_models(processes)
        runtimes.append({"id": runtime, "name": NAMES[runtime], "running": bool(processes),
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
        "primary": next(({"runtime": runtime["name"], "model": next((model["name"] for model in runtime["models"] if model["state"] != "sleeping"), "")}
                         for runtime in sorted(loaded, key=lambda r: -r["vram"])), None),
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
