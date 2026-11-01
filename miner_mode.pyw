"""Miner Mode: one-click switch for an SRBMiner-Multi rig with two NVIDIA cards on Windows.

  Both cards   -> runs your full-rig .bat (your overclock)
  One card     -> resets the freed card to stock, then runs your one-card .bat (gaming / local LLM)
  Stop + reset -> stops mining and resets every card to stock

SRBMiner leaves its overclock applied after it exits. "Reset" briefly runs SRBMiner with --gpu-reset-oc on
just the cards being freed and stops it as soon as nvidia-smi shows their default power limit.
Settings live in config.json next to this file (copy config.example.json).
"""
import ctypes
import json
import os
import subprocess
import threading
import time
import tkinter as tk
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def load_config(path=os.path.join(HERE, "config.json")):
    if not os.path.exists(path):
        raise SystemExit(f"Missing {path}: copy config.example.json to config.json and fill it in.")
    c = json.load(open(path, encoding="utf-8"))
    c.setdefault("exe", "SRBMiner-MULTI.exe")
    c.setdefault("api", "http://127.0.0.1:21550")
    c.setdefault("loop_match", "start-mining")      # text in the .bat names, to find the running loop
    c.setdefault("watchdog_match", "")              # text in a watchdog's command line to stop too ("" = none)
    return c


def run(args):
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)


def ps(cmd):
    return run(["powershell", "-NoProfile", "-Command", cmd]).stdout


def power_limits():
    """[(current W, default W)] per GPU, from nvidia-smi."""
    out = run(["nvidia-smi", "--query-gpu=power.limit,power.default_limit", "--format=csv,noheader,nounits"]).stdout
    return [tuple(float(x) for x in line.split(",")) for line in out.strip().splitlines()]


def at_stock(limits, gpus):
    """Pure: True when every listed GPU's power limit equals its default."""
    return all(abs(limits[g][0] - limits[g][1]) < 1 for g in gpus)


def mode_of(cmdlines, cfg):
    """Pure: which loop is running, from cmd.exe command lines -> 'both', 'single' or None."""
    single = os.path.basename(cfg["single_bat"]).lower()
    both = os.path.basename(cfg["both_bat"]).lower()
    lines = [c.lower() for c in cmdlines]
    if any(single in c for c in lines):
        return "single"
    if any(both in c for c in lines):
        return "both"
    return None


class Rig:
    def __init__(self, cfg):
        self.c = cfg

    def cmd_loops(self):
        return ps("Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { $_.CommandLine }").splitlines()

    def mode(self):
        return mode_of(self.cmd_loops(), self.c)

    def stop(self):
        """Stop the .bat loop first (else it restarts the miner), then the watchdog, then every miner process."""
        m, w = self.c["loop_match"], self.c["watchdog_match"]
        ps("Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*" + m + "*')"
           + (" -or ($_.Name -like 'python*.exe' -and $_.CommandLine -like '*" + w + "*')" if w else "")
           + " } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        run(["taskkill", "/IM", self.c["exe"], "/F", "/T"])
        time.sleep(3)

    def reset(self, gpus, say=print, timeout=40):
        ids = ",".join(str(g) for g in gpus)
        say(f"Resetting GPU {ids} to stock…")
        p = subprocess.Popen([os.path.join(self.c["miner_dir"], self.c["exe"]), "--algorithm-gpu", self.c["algorithm"],
                              "--pool", self.c["pool"], "--wallet", self.c["wallet"], "--gpu-id", ids,
                              "--gpu-reset-oc", "--disable-worker-watchdog"], cwd=self.c["miner_dir"], creationflags=NO_WINDOW)
        ok = False
        for _ in range(timeout):
            time.sleep(1)
            if at_stock(power_limits(), gpus):
                ok = True
                break
        time.sleep(2)
        p.kill()
        run(["taskkill", "/IM", self.c["exe"], "/F", "/T"])
        return ok

    @staticmethod
    def launch(bat):
        subprocess.Popen(["explorer.exe", bat])   # a real console, like a double-click (`timeout` needs one)

    def stats(self):
        """'gpu0 91.7 TH/s' lines from the SRBMiner API, or None when it isn't answering."""
        try:
            j = json.load(urllib.request.urlopen(self.c["api"], timeout=3))
        except Exception:
            return None
        a = j["algorithms"][0]
        names = {f"gpu{d['id']}": d["model"].replace("nvidia_geforce_", "").replace("_", " ") for d in j.get("gpu_devices", [])}
        rows = [f"{names.get(k, k)[:12]:12} {v / 1e12:6.1f} TH/s" for k, v in sorted(a["hashrate"]["gpu"].items()) if k != "total"]
        return "\n".join(rows) + f"\nshares {a['shares']['accepted']} ok / {a['shares']['rejected']} rejected"


class App:
    def __init__(self, root, rig):
        self.root, self.rig, c = root, rig, rig.c
        root.title("Miner Mode")
        root.resizable(False, False)
        root.configure(bg="#161d2a", padx=18, pady=16)
        f = ("Segoe UI", 11)
        self.mode = tk.Label(root, text="…", font=("Segoe UI Semibold", 14), fg="#e6eaf2", bg="#161d2a")
        self.mode.grid(row=0, column=0, columnspan=3, sticky="w")
        self.stat = tk.Label(root, text="", font=("Consolas", 10), fg="#98a2b5", bg="#161d2a", justify="left")
        self.stat.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))
        free = ", ".join(c["names"][str(g)] for g in c["free_gpus"])
        keep = c["names"][str(c["single_gpu"])]
        self.btns = [
            tk.Button(root, text="Both cards\n(full mining)", font=f, width=15, bg="#1f7a4d", fg="white",
                      activebackground="#25915c", relief="flat", command=lambda: self.go(self.both)),
            tk.Button(root, text=f"{keep} only\n(free the {free})", font=f, width=15, bg="#b7791f", fg="white",
                      activebackground="#cf8a24", relief="flat", command=lambda: self.go(self.single)),
            tk.Button(root, text="Stop mining\n+ reset all", font=f, width=15, bg="#c0392b", fg="white",
                      activebackground="#d64535", relief="flat", command=lambda: self.go(self.stop_all)),
        ]
        for i, b in enumerate(self.btns):
            b.grid(row=2, column=i, padx=4)
        self.msg = tk.Label(root, text="", font=("Segoe UI", 10), fg="#f0b450", bg="#161d2a")
        self.msg.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.busy = False
        self.labels = {"both": "Mining: both cards", "single": f"Mining: {keep} only ({free} free)", None: "Not mining"}
        self.refresh()

    def say(self, text):
        self.root.after(0, lambda: self.msg.config(text=text))

    def go(self, fn):
        if self.busy:          # a second click while a switch runs is ignored
            return
        self.busy = True
        for b in self.btns:
            b.config(state="disabled")

        def work():
            try:
                fn()
            except Exception as e:
                self.say(f"Error: {e}")
            finally:
                self.busy = False
                self.root.after(0, lambda: [b.config(state="normal") for b in self.btns])
        threading.Thread(target=work, daemon=True).start()

    def both(self):
        self.say("Stopping current miner…")
        self.rig.stop()
        self.rig.launch(self.rig.c["both_bat"])
        self.say("Started both cards. Hashrate shows in about a minute.")

    def single(self):
        self.say("Stopping current miner…")
        self.rig.stop()
        ok = self.rig.reset(self.rig.c["free_gpus"], self.say)
        self.rig.launch(self.rig.c["single_bat"])
        self.say("Freed card reset to stock; one card mining." if ok else "One card mining; reset NOT confirmed: check Afterburner.")

    def stop_all(self):
        self.say("Stopping mining…")
        self.rig.stop()
        ok = self.rig.reset(list(range(len(power_limits()))), self.say)
        self.say("Mining stopped, all cards at stock." if ok else "Mining stopped; reset NOT confirmed: check Afterburner.")

    def refresh(self):
        def work():
            m, s = self.rig.mode(), self.rig.stats()
            txt = s or "miner not answering (starting, stopped or switching)"
            txt += "\npower limit: " + "  ".join(f"GPU{i} {p[0]:.0f}/{p[1]:.0f} W" for i, p in enumerate(power_limits()))
            self.root.after(0, lambda: (self.mode.config(text=self.labels[m]), self.stat.config(text=txt)))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(5000, self.refresh)


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, "MinerModeApp")   # one window only
    if ctypes.windll.kernel32.GetLastError() == 183:                    # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "Miner Mode is already open.", "Miner Mode", 0x40)
        return
    root = tk.Tk()
    App(root, Rig(load_config()))
    root.mainloop()


if __name__ == "__main__":
    main()
