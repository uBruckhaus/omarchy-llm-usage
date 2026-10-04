# LLM Monitor

![LLM Monitor — a local LLM monitor for your Omarchy bar](preview.png)

**See at a glance when a local LLM is using your GPU, how much it holds, and who is using it.**

LLM Monitor is a small Omarchy bar widget for people who run local models with **llama.cpp**, **Ollama** or **LM Studio**.

- **Bar indicator:** a chip icon. Dim while no model is loaded. While a model is resident it turns into the accent colour and shows the GPU memory held by local models (e.g. `󰘚 8.7G`), and it pulses while the GPU is generating. A `!` marks model memory that no longer fits in VRAM and was moved to system RAM.
- **Detail panel (left click):**
  - **GPU memory** as one stacked bar with a legend: *LLM models* (the number in the bar), *Desktop & apps* (with the programs behind it, e.g. Hyprland, browser) and *Free*, so the bar number never looks like a mismatch with the GPU total.
  - **GPU activity:** load, temperature and power.
  - **System:** CPU and RAM.
  - **Runtimes:** llama.cpp, Ollama and LM Studio, each with state (stopped / running / model loaded / model sleeping), model name and context size, VRAM, CPU, RAM and the programs currently connected (e.g. pi, Simple Recipes).
  - **Free GPU memory** button and a **btop** shortcut.
- **Right click:** opens btop.

## Install

Requires Omarchy Quattro's plugin-capable Quickshell and `python3` (standard library only, no extra packages). No setup script is needed.

```sh
omarchy plugin add https://github.com/uBruckhaus/omarchy-llm-usage
omarchy plugin enable ubruckhaus.llm-usage
```

GPU memory, load, temperature and power are read from the amdgpu sysfs and DRM `fdinfo` interfaces (AMD GPUs). On other GPUs the runtime and system information still works; GPU values may be missing.

## How it works

A collector (`scripts/llm-usage.py`) runs while the widget is loaded and streams one snapshot every two seconds (about 1–2 % of one CPU core, ~25 MiB RAM).

- **Read-only by default.** It reads `/proc`, sysfs and the runtimes' local status endpoints on `127.0.0.1` (ports 8080, 11434, 1234) **only while that runtime is already running**. It never starts, stops or reconfigures a runtime by itself and sends nothing to the network.
- **Runtimes are told apart by systemd unit and executable path**, because Ollama and LM Studio both ship an engine named `llama-server`.
- **GPU memory per runtime** comes from DRM `fdinfo` (`drm-resident-vram`), so only memory that is really in VRAM is counted; requested-but-evicted memory is reported separately.

### Free GPU memory (the only action that changes anything)

Only when you click **Free GPU memory**, `scripts/free-gpu.sh` runs:

- `systemctl --user stop llama-server.service ollama.service` (user units only, no root),
- `lms unload --all` if the LM Studio daemon is running (its daemon keeps running).

This stops those local AI services for every program using them at that moment.

## Update or remove

```sh
omarchy plugin update ubruckhaus.llm-usage
```

```sh
omarchy plugin disable ubruckhaus.llm-usage
omarchy plugin remove ubruckhaus.llm-usage
```

The plugin creates no files, services or settings outside its own folder, so nothing else needs cleaning up.

## Development

```sh
python3 scripts/llm-usage.py --once | python3 -m json.tool
omarchy plugin validate .
```

`preview.png` is a composed screenshot of the real panel while Ollama was generating (see `PREVIEW.md`).

Licensed under [MIT](LICENSE).
