"""Miner Mode: switch an SRBMiner-Multi rig between mining profiles in one click, with every freed GPU put back to
stock clocks, a per-GPU health table, auto-switching when games run, and a command line for schedulers.

  GUI:  pythonw miner_mode.pyw
  CLI:  python miner_mode.pyw --status [--json] | --profile "NAME" | --stop | --reset 0,3,7

SRBMiner leaves its overclock applied after it exits. Freed cards are reset by a short SRBMiner run with
--gpu-reset-oc on just those cards, confirmed when nvidia-smi shows each card's default power limit again.
GPUs are matched by PCI bus address, because SRBMiner's numbering and nvidia-smi's can differ on big rigs.
Settings: config.json next to this file (see config.example.json).
"""
import argparse
import ctypes
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STOP = "__stop__"                     # pseudo-profile: stop mining, reset every card
SMI_FIELDS = "index,pci.bus_id,name,temperature.gpu,fan.speed,power.draw,power.limit,power.default_limit,utilization.gpu"


# ---------------------------------------------------------------- config

class ConfigError(Exception):
    pass


def load_config(path=os.path.join(HERE, "config.json")):
    if not os.path.exists(path):
        raise ConfigError(f"Missing {path}.\nCopy config.example.json to config.json and fill it in.")
    try:
        c = json.load(open(path, encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"config.json is not valid JSON: {e}\n(Windows paths need double backslashes: C:\\\\Users\\\\...)")
    return normalise(c)


def normalise(c):
    """Fill defaults, accept the old two-.bat format, and check required keys. Pure."""
    c = dict(c)
    for k, v in {"exe": "SRBMiner-MULTI.exe", "api": "http://127.0.0.1:21550", "loop_match": "start-mining",
                 "watchdog_match": "", "poll_s": 5, "hot_c": 83, "names": {}, "log_file": "miner_mode.log",
                 "auto": {}}.items():
        c.setdefault(k, v)
    if "profiles" not in c and "both_bat" in c:          # v1 config: one full and one single-card .bat
        n = c["names"]
        c["profiles"] = [{"name": "Both cards", "bat": c["both_bat"], "gpus": "all"},
                         {"name": f"{n.get(str(c.get('single_gpu')), 'GPU ' + str(c.get('single_gpu')))} only",
                          "bat": c["single_bat"], "gpus": [c.get("single_gpu")]}]
    missing = [k for k in ("miner_dir", "algorithm", "pool", "wallet", "profiles") if not c.get(k)]
    if missing:
        raise ConfigError("config.json is missing: " + ", ".join(missing))
    for p in c["profiles"]:
        if not (p.get("name") and p.get("bat")) or not (p.get("gpus") == "all" or isinstance(p.get("gpus"), list)):
            raise ConfigError(f"Each profile needs name, bat and gpus (\"all\" or a list of GPU ids): {p}")
    a = c["auto"]
    a.setdefault("enabled", False)
    a.setdefault("apps", [])
    a.setdefault("back_after_s", 120)
    names = {p["name"] for p in c["profiles"]}
    for k in ("profile", "back_to"):
        if a.get(k) and a[k] not in names:
            raise ConfigError(f"auto.{k} = {a[k]!r} is not a profile name ({', '.join(sorted(names))})")
    return c


# ---------------------------------------------------------------- pure helpers (tested in test_miner_mode.py)

def norm_bus(s):
    """'00000000:01:00.0' / '0000:01:00.0' / '0x01' -> '01:00.0'."""
    s = s.strip().lower()
    m = re.search(r"([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$", s)
    if m:
        return f"{m.group(1)}:{m.group(2)}.{m.group(3)}"
    return f"{int(s, 16):02x}:00.0" if re.fullmatch(r"(0x)?[0-9a-f]+", s) else s


def parse_list_devices(text):
    """SRBMiner --list-devices -> {srb_id: bus}. Lines look like 'GPU0  [CUDA][0] [0000:01:00.0] : nvidia_...'."""
    return {int(i): norm_bus(b) for i, b in re.findall(r"^\s*GPU(\d+)\s.*?\[([0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d)\]",
                                                        text, re.M)}


def parse_smi(text):
    """nvidia-smi CSV (SMI_FIELDS, noheader, nounits) -> [dict] keyed by bus; '[N/A]' -> None."""
    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    out = []
    for r in csv.reader(io.StringIO(text.strip())):
        if len(r) < 9:
            continue
        r = [x.strip() for x in r]
        out.append({"idx": int(r[0]), "bus": norm_bus(r[1]), "name": r[2].replace("NVIDIA GeForce ", ""),
                    "temp": num(r[3]), "fan": num(r[4]), "draw": num(r[5]), "pl": num(r[6]), "pl_def": num(r[7]),
                    "util": num(r[8])})
    return out


def api_hashrates(j):
    """SRBMiner API JSON -> {bus: TH/s} for the first algorithm, mapped through gpu_devices[].topology_id."""
    bus = {f"gpu{d['id']}": norm_bus(d.get("topology_id", "")) for d in j.get("gpu_devices", [])}
    a = j["algorithms"][0]
    return {bus.get(k, k): v / 1e12 for k, v in a["hashrate"]["gpu"].items() if k != "total"}


def at_stock(g):
    return g["pl"] is not None and g["pl_def"] is not None and abs(g["pl"] - g["pl_def"]) < 1


def mode_of(cmdlines, profiles):
    """Which profile's .bat loop is running, from cmd.exe command lines; longest .bat name wins. Pure."""
    lines = [c.lower() for c in cmdlines]
    for p in sorted(profiles, key=lambda p: -len(os.path.basename(p["bat"]))):
        if any(os.path.basename(p["bat"]).lower() in c for c in lines):
            return p["name"]
    return None


def profile_buses(profile, all_buses, srb2bus):
    """Buses a profile mines on. 'all' = every GPU nvidia-smi sees; ids are SRBMiner ids."""
    if profile is None:
        return set()
    if profile["gpus"] == "all":
        return set(all_buses)
    return {srb2bus[i] for i in profile["gpus"] if i in srb2bus}


def buses_to_reset(gpus, keep):
    """After stopping: every card the next profile won't use and that isn't already at stock. Pure."""
    return [g["bus"] for g in gpus if g["bus"] not in keep and not at_stock(g)]


def health(g, ths, mining, hot_c):
    """Flags for one GPU row. ths None = no API data for it."""
    f = []
    if g["temp"] is not None and g["temp"] >= hot_c:
        f.append("HOT")
    if mining and ths is not None and ths <= 0:
        f.append("0 H/s")
    if not mining and not at_stock(g):
        f.append("OC left on")
    return f


class AutoSwitch:
    """Switch to `profile` while any watched app runs; back to `back_to` (or the previous profile) once they've
    been closed for `back_after_s`. Only undoes its own switch; a manual click cancels it. Pure (time passed in)."""

    def __init__(self, cfg):
        self.apps = {a.lower() for a in cfg.get("apps", [])}
        self.profile, self.back_to, self.after = cfg.get("profile"), cfg.get("back_to"), cfg.get("back_after_s", 120)
        self.active, self.prev, self.gone_since = False, None, None

    def cancel(self):
        self.active, self.gone_since = False, None

    def step(self, now, running, mode):
        """running: lowercase process names. Returns a profile name to switch to, or None."""
        if not (self.apps and self.profile):
            return None
        if self.apps & running:
            self.gone_since = None
            if not self.active and mode != self.profile:
                self.active, self.prev = True, mode
                return self.profile
            return None
        if self.active:
            self.gone_since = self.gone_since or now
            if now - self.gone_since >= self.after:
                self.cancel()
                back = self.back_to or self.prev
                return back if back and back != mode else None
        return None


# ---------------------------------------------------------------- the rig

class SwitchLock:
    """Named mutex so the GUI, the CLI and a scheduled task never switch at the same time."""

    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.h = self.k.CreateMutexW(None, False, "MinerModeSwitchLock")

    def __enter__(self):
        if self.k.WaitForSingleObject(self.h, 300_000) not in (0, 0x80):   # signalled or abandoned = ours
            raise RuntimeError("another switch is still running (waited 5 min)")
        return self

    def __exit__(self, *a):
        self.k.ReleaseMutex(self.h)


def run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW, timeout=timeout)


def ps(cmd):
    return run(["powershell", "-NoProfile", "-Command", cmd]).stdout


class Rig:
    def __init__(self, cfg, say=print):
        self.c, self.say = cfg, say
        self.exe = os.path.join(cfg["miner_dir"], cfg["exe"])
        self._srb2bus = None

    # -- reading
    def gpus(self):
        try:
            return parse_smi(run(["nvidia-smi", f"--query-gpu={SMI_FIELDS}", "--format=csv,noheader,nounits"], 30).stdout)
        except (OSError, subprocess.TimeoutExpired):
            return []

    def srb2bus(self, refresh=False):
        """SRBMiner id -> bus, from --list-devices (cached; it takes a few seconds)."""
        if self._srb2bus is None or refresh:
            try:
                self._srb2bus = parse_list_devices(run([self.exe, "--list-devices"], 60).stdout)
            except (OSError, subprocess.TimeoutExpired):
                self._srb2bus = {}
        return self._srb2bus

    def hashrates(self):
        try:
            return api_hashrates(json.load(urllib.request.urlopen(self.c["api"], timeout=3)))
        except Exception:
            return None

    def mode(self):
        return mode_of(ps("Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { $_.CommandLine }")
                       .splitlines(), self.c["profiles"])

    def profile(self, name):
        return next((p for p in self.c["profiles"] if p["name"] == name), None)

    # -- acting
    def stop(self):
        """.bat loop first (else it restarts the miner), then the watchdog, then every miner process."""
        m, w = self.c["loop_match"], self.c["watchdog_match"]
        ps("Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*" + m + "*')"
           + (" -or ($_.Name -like 'python*.exe' -and $_.CommandLine -like '*" + w + "*')" if w else "")
           + " } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        run(["taskkill", "/IM", self.c["exe"], "/F", "/T"])
        time.sleep(3)

    def reset(self, buses):
        """Reset these buses to stock in one SRBMiner run. Returns the buses still not at stock."""
        if not buses:
            return []
        b2s = {b: i for i, b in self.srb2bus().items()}
        ids = sorted(b2s[b] for b in buses if b in b2s)
        if not ids:
            return list(buses)
        self.say(f"Resetting {len(ids)} GPU(s) to stock...")
        p = subprocess.Popen([self.exe, "--algorithm-gpu", self.c["algorithm"], "--pool", self.c["pool"],
                              "--wallet", self.c["wallet"], "--gpu-id", ",".join(map(str, ids)), "--gpu-reset-oc",
                              "--disable-worker-watchdog"], cwd=self.c["miner_dir"], creationflags=NO_WINDOW)
        left = set(buses)
        end = time.time() + 40 + 0.5 * len(ids)          # big rigs take longer to initialise
        while left and time.time() < end and p.poll() is None:
            time.sleep(1)
            left -= {g["bus"] for g in self.gpus() if at_stock(g)}
        time.sleep(2)
        p.kill()
        run(["taskkill", "/IM", self.c["exe"], "/F", "/T"])
        return sorted(left)

    def switch(self, name):
        """Stop, reset every card the new profile won't mine on, start its .bat. name=STOP: stop + reset all."""
        target = None if name == STOP else self.profile(name)
        if name != STOP and target is None:
            raise ValueError(f"no profile named {name!r}")
        with SwitchLock():
            self.say("Stopping current miner...")
            self.stop()
            gpus = self.gpus()
            keep = profile_buses(target, [g["bus"] for g in gpus], self.srb2bus())
            failed = self.reset(buses_to_reset(gpus, keep))
            if target:
                subprocess.Popen(["explorer.exe", target["bat"]])   # a real console, like a double-click
            msg = (f"{target['name']}: started." if target else "Mining stopped.") + \
                  (f" Reset NOT confirmed on {len(failed)} GPU(s): {', '.join(failed)}" if failed else "")
            self.log(f"switch {name} -> {msg}")
            self.say(msg)
            return not failed

    def reset_selected(self, buses):
        with SwitchLock():
            failed = self.reset(list(buses))
            msg = f"Reset {len(buses) - len(failed)}/{len(buses)} GPU(s) to stock." + (f" Failed: {', '.join(failed)}" if failed else "")
            self.log(msg)
            self.say(msg)
            return not failed

    def log(self, msg):
        try:
            with open(os.path.join(HERE, self.c["log_file"]), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    def status(self):
        gpus, hr, mode = self.gpus(), self.hashrates(), self.mode()
        mining = profile_buses(self.profile(mode), [g["bus"] for g in gpus], self.srb2bus()) if mode else set()
        rows = []
        for g in gpus:
            ths = (hr or {}).get(g["bus"])
            rows.append({**g, "mining": g["bus"] in mining, "ths": ths, "flags": health(g, ths, g["bus"] in mining, self.c["hot_c"])})
        return {"mode": mode, "total_ths": round(sum(r["ths"] or 0 for r in rows), 2), "gpus": rows}


def running_processes():
    out = run(["tasklist", "/fo", "csv", "/nh"]).stdout
    return {r[0].lower() for r in csv.reader(io.StringIO(out)) if r}


# ---------------------------------------------------------------- GUI

def gui(cfg):
    import tkinter as tk
    from tkinter import ttk

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)       # plain windll can lose the error code
    _mutex = k32.CreateMutexW(None, False, "MinerModeApp")     # one window only
    if ctypes.get_last_error() == 183:                         # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "Miner Mode is already open.", "Miner Mode", 0x40)
        return

    BG, FG, MUTED = "#161d2a", "#e6eaf2", "#98a2b5"
    root = tk.Tk()
    root.title("Miner Mode")
    root.configure(bg=BG, padx=16, pady=14)
    root.minsize(760, 420)
    st = ttk.Style()
    st.theme_use("clam")
    st.configure("Treeview", background="#1d2636", fieldbackground="#1d2636", foreground=FG, rowheight=24, borderwidth=0)
    st.configure("Treeview.Heading", background="#243045", foreground=MUTED, relief="flat")
    st.map("Treeview", background=[("selected", "#33507a")])

    head = tk.Label(root, text="...", font=("Segoe UI Semibold", 15), fg=FG, bg=BG, anchor="w")
    head.pack(fill="x")
    summary = tk.Label(root, text="", font=("Segoe UI", 10), fg=MUTED, bg=BG, anchor="w")
    summary.pack(fill="x", pady=(0, 8))

    cols = ("gpu", "card", "state", "ths", "temp", "fan", "power", "limit", "flags")
    heads = ("GPU", "Card", "State", "TH/s", "°C", "Fan", "Power", "Limit / stock", "Health")
    widths = (48, 150, 72, 70, 50, 52, 70, 110, 120)
    frame = tk.Frame(root, bg=BG)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=cols, show="headings", height=8, selectmode="extended")
    for c_, h, w in zip(cols, heads, widths):
        tree.heading(c_, text=h)
        tree.column(c_, width=w, anchor="e" if c_ in ("ths", "temp", "fan", "power") else "w", stretch=c_ == "card")
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.tag_configure("mining", foreground="#7ee2a8")
    tree.tag_configure("free", foreground=MUTED)
    tree.tag_configure("warn", foreground="#f0b450")
    tree.tag_configure("bad", foreground="#f07167")

    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", pady=(12, 4))
    rig = Rig(cfg, say=lambda m: root.after(0, lambda: msg.config(text=m)))
    auto = AutoSwitch(cfg["auto"])
    auto_on = tk.BooleanVar(value=cfg["auto"]["enabled"])
    state = {"busy": False, "mode": None, "rows": []}
    buttons = []
    colours = ["#1f7a4d", "#b7791f", "#2f6db5", "#7a4fb5", "#2a8f8f", "#8f6a2a"]

    def go(fn, manual=True):
        if state["busy"]:                 # ignore clicks while a switch runs
            return
        if manual:
            auto.cancel()                 # a manual choice beats auto-switch
        state["busy"] = True
        for b in buttons:
            b.config(state="disabled")

        def work():
            try:
                fn()
            except Exception as e:
                rig.say(f"Error: {e}")
                rig.log(f"error: {e!r}")
            finally:
                state["busy"] = False
                root.after(0, lambda: [b.config(state="normal") for b in buttons])
        threading.Thread(target=work, daemon=True).start()

    for i, p in enumerate(cfg["profiles"]):
        b = tk.Button(bar, text=p["name"], font=("Segoe UI", 11), bg=colours[i % len(colours)], fg="white", relief="flat",
                      padx=14, pady=6, activebackground="#3a4a63", command=lambda n=p["name"]: go(lambda: rig.switch(n)))
        b.pack(side="left", padx=(0, 6))
        buttons.append(b)
    b = tk.Button(bar, text="Stop + reset all", font=("Segoe UI", 11), bg="#c0392b", fg="white", relief="flat",
                  padx=14, pady=6, activebackground="#d64535", command=lambda: go(lambda: rig.switch(STOP)))
    b.pack(side="left", padx=(0, 6))
    buttons.append(b)

    def reset_sel():
        sel = list(tree.selection())
        busy = [s for s in sel if any(r["bus"] == s and r["mining"] for r in state["rows"])]
        if busy:
            rig.say("Selected GPUs that are mining can't be reset: switch profile first.")
            return
        if sel:
            go(lambda: rig.reset_selected(sel))
        else:
            rig.say("Select one or more free GPUs in the table first (Ctrl/Shift-click).")
    b = tk.Button(bar, text="Reset selected", font=("Segoe UI", 11), bg="#3a4a63", fg="white", relief="flat",
                  padx=14, pady=6, command=reset_sel)
    b.pack(side="right")
    buttons.append(b)

    low = tk.Frame(root, bg=BG)
    low.pack(fill="x", pady=(6, 0))
    apps = ", ".join(cfg["auto"]["apps"]) or "no apps configured"
    tk.Checkbutton(low, text=f"Auto-switch to “{cfg['auto'].get('profile') or '—'}” while running: {apps}",
                   variable=auto_on, bg=BG, fg=MUTED, selectcolor=BG, activebackground=BG, activeforeground=FG,
                   font=("Segoe UI", 9), state="normal" if cfg["auto"].get("profile") else "disabled").pack(side="left")
    msg = tk.Label(root, text="", font=("Segoe UI", 10), fg="#f0b450", bg=BG, anchor="w")
    msg.pack(fill="x", pady=(6, 0))

    def draw(s):
        state["mode"], state["rows"] = s["mode"], s["gpus"]
        rows = s["gpus"]
        mining = sum(r["mining"] for r in rows)
        warn = sum(bool(r["flags"]) for r in rows)
        head.config(text=f"Mining: {s['mode']}" if s["mode"] else "Not mining")
        summary.config(text=f"{len(rows)} GPUs · {mining} mining · {len(rows) - mining} free · {s['total_ths']:.1f} TH/s total"
                            + (f" · ⚠ {warn} need attention" if warn else ""))
        seen = set()
        for r in rows:
            seen.add(r["bus"])
            vals = (r["idx"], r["name"], "mining" if r["mining"] else "free",
                    "—" if r["ths"] is None else f"{r['ths']:.1f}",
                    "—" if r["temp"] is None else f"{r['temp']:.0f}", "—" if r["fan"] is None else f"{r['fan']:.0f}%",
                    "—" if r["draw"] is None else f"{r['draw']:.0f} W",
                    f"{r['pl'] or 0:.0f} / {r['pl_def'] or 0:.0f} W", ", ".join(r["flags"]) or "ok")
            tag = "bad" if {"HOT", "0 H/s"} & set(r["flags"]) else "warn" if r["flags"] else "mining" if r["mining"] else "free"
            if tree.exists(r["bus"]):
                tree.item(r["bus"], values=vals, tags=(tag,))
            else:
                tree.insert("", "end", iid=r["bus"], values=vals, tags=(tag,))
        for iid in set(tree.get_children()) - seen:
            tree.delete(iid)

    def poll():
        def work():
            try:
                s = rig.status()
                root.after(0, lambda: draw(s))
                if auto_on.get() and not state["busy"]:
                    target = auto.step(time.time(), running_processes(), s["mode"])
                    if target:
                        rig.say(f"Auto-switch → {target}")
                        rig.log(f"auto-switch -> {target}")
                        root.after(0, lambda: go(lambda: rig.switch(target), manual=False))
            except Exception as e:
                rig.say(f"Status error: {e}")
        threading.Thread(target=work, daemon=True).start()
        root.after(int(cfg["poll_s"] * 1000), poll)

    threading.Thread(target=rig.srb2bus, daemon=True).start()      # warm the id map before the first click
    poll()
    root.mainloop()


# ---------------------------------------------------------------- CLI

def cli(cfg, a):
    rig = Rig(cfg)
    if a.status:
        s = rig.status()
        if a.json:
            print(json.dumps(s, indent=1))
        else:
            print(f"Mode: {s['mode'] or 'not mining'} | {s['total_ths']:.1f} TH/s")
            for r in s["gpus"]:
                ths = "" if r["ths"] is None else f"{r['ths']:.1f} TH/s"   # no nested f-strings: Python 3.10 compatible
                print(f"  GPU{r['idx']:<3} {r['bus']}  {r['name']:<22} {'mining' if r['mining'] else 'free':7} "
                      f"{ths:>11}  {r['temp'] or 0:.0f}C  {r['pl'] or 0:.0f}/{r['pl_def'] or 0:.0f} W  "
                      f"{', '.join(r['flags']) or 'ok'}")
        return 0
    if a.reset:
        g = {x["idx"]: x["bus"] for x in rig.gpus()}
        return 0 if rig.reset_selected([g[int(i)] for i in a.reset.split(",") if int(i) in g]) else 1
    return 0 if rig.switch(STOP if a.stop else a.profile) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Miner Mode: SRBMiner profile switcher. No arguments = open the window.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--profile", help="switch to this profile (name from config.json)")
    g.add_argument("--stop", action="store_true", help="stop mining and reset every GPU to stock")
    g.add_argument("--reset", metavar="IDS", help="reset these nvidia-smi GPU indexes to stock, e.g. 0,3,7")
    g.add_argument("--status", action="store_true", help="print mode and per-GPU status")
    ap.add_argument("--json", action="store_true", help="with --status: machine-readable output")
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        if not any((a.profile, a.stop, a.reset, a.status)):
            ctypes.windll.user32.MessageBoxW(None, str(e), "Miner Mode: config problem", 0x10)
        else:
            print(e, file=sys.stderr)
        return 2
    if any((a.profile, a.stop, a.reset, a.status)):
        return cli(cfg, a)
    gui(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
