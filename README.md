# Miner Mode

A one-window Windows app that switches an [SRBMiner-Multi](https://github.com/doktor83/SRBMiner-Multi) rig with two NVIDIA cards between:

| Button | What it does |
|---|---|
| **Both cards** | Stops whatever is running and starts your full-rig `.bat` (your overclock) |
| **One card only** | Stops mining, **resets the freed card to stock**, then starts your one-card `.bat`. Use it to game or run a local LLM on the other card |
| **Stop mining + reset all** | Stops everything and puts every card back to stock |

It also shows live per-card hashrate and shares (from SRBMiner's API) and each card's power limit next to its stock value, refreshed every 5 s.

## Why the reset

SRBMiner leaves its overclock (core/memory offsets, power limit, fan) applied after it exits, so a card you free for a game keeps the mining settings until you reset it in MSI Afterburner. Miner Mode does that for you: it briefly runs SRBMiner with `--gpu-reset-oc --gpu-id <card>` and stops it as soon as `nvidia-smi` shows the card's default power limit. If that doesn't happen within 40 s, it tells you to check Afterburner.

## Setup

Needs Windows, Python 3.10+ (tkinter is included), NVIDIA drivers (`nvidia-smi`) and SRBMiner-Multi.

1. In SRBMiner's folder, keep two `.bat` files that loop the miner, each with `--api-enable` (the full rig, and the single card with `--gpu-id <n>`). Their names must contain the `loop_match` text.
2. Copy `config.example.json` to `config.json` and fill in your paths, pool and wallet. `config.json` is git-ignored.
3. Make a desktop shortcut: target `pythonw.exe "<path>\miner_mode.pyw"`.

| Config key | Meaning |
|---|---|
| `miner_dir` | SRBMiner folder |
| `both_bat`, `single_bat` | The two `.bat` files |
| `algorithm`, `pool`, `wallet` | Used only for the short reset run |
| `free_gpus` | GPU ids reset in one-card mode (e.g. `[0]`) |
| `single_gpu` | The GPU that keeps mining |
| `names` | Button labels per GPU id |
| `loop_match` | Text in the `.bat` names, to find and stop the running loop |
| `watchdog_match` | Optional: text in a watchdog's command line, stopped before a switch |

## Notes

- Only one window opens at a time; a second launch says "already open".
- A second click while a switch is running is ignored.
- `.bat` files are started through Explorer so they get a real console (`timeout` fails without one).
- Self-check of the pure logic: `python test_miner_mode.py`.
