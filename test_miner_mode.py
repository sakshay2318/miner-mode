"""Self-check of the pure logic, no GPU or miner needed: python test_miner_mode.py"""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mm", os.path.join(HERE, "miner_mode.pyw"))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

# bus addresses: three spellings, one key
assert mm.norm_bus("00000000:01:00.0") == mm.norm_bus("0000:01:00.0") == "01:00.0"
assert mm.norm_bus("0x0A") == "0a:00.0"

# SRBMiner --list-devices (real 3.8.3 output) and a 100-GPU rig where SRBMiner and nvidia-smi order differ
real = ("GPU0  [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_3070_ti [ampere] [CC: 8.6] [SM: 48] [8191 MB]\n"
        "GPU1  [CUDA][1] [0000:02:00.0] : nvidia_geforce_rtx_3060 [ampere] [CC: 8.6] [SM: 28] [12287 MB]\n")
assert mm.parse_list_devices(real) == {0: "01:00.0", 1: "02:00.0"}
big = "".join(f"GPU{i}  [CUDA][{i}] [0000:{(99 - i) + 1:02x}:00.0] : card\n" for i in range(100))
m = mm.parse_list_devices(big)
assert len(m) == 100 and m[0] == "64:00.0" and m[99] == "01:00.0"

# nvidia-smi CSV, incl. [N/A] fields
smi = mm.parse_smi("0, 00000000:01:00.0, NVIDIA GeForce RTX 3070 Ti, 64, 100, 179.78, 180.00, 290.00, 100\n"
                   "1, 00000000:02:00.0, NVIDIA GeForce RTX 3060, 55, [N/A], 111.93, 170.00, 170.00, 100\n")
assert smi[0]["name"] == "RTX 3070 Ti" and smi[0]["bus"] == "01:00.0" and smi[1]["fan"] is None
assert not mm.at_stock(smi[0]) and mm.at_stock(smi[1])

# API: SRBMiner's gpuN -> bus through topology_id
api = {"gpu_devices": [{"id": 0, "topology_id": "0000:02:00.0"}, {"id": 1, "topology_id": "0000:01:00.0"}],
       "algorithms": [{"hashrate": {"gpu": {"gpu0": 5e13, "gpu1": 9e13, "total": 1.4e14}}}]}
assert mm.api_hashrates(api) == {"02:00.0": 50.0, "01:00.0": 90.0}

# profiles and mode detection (longest .bat name wins; case; cmd /K)
cfg = mm.normalise({"miner_dir": "C:\\m", "algorithm": "pearlhash", "pool": "p:1", "wallet": "w",
                    "profiles": [{"name": "All", "bat": r"C:\m\start-mining-pearl.bat", "gpus": "all"},
                                 {"name": "One", "bat": r"C:\m\start-mining-pearl-3060-only.bat", "gpus": [1]}],
                    "auto": {"apps": ["Game.exe"], "profile": "One", "back_to": "All", "back_after_s": 60}})
P = cfg["profiles"]
assert mm.mode_of([r'cmd.exe /c ""C:\m\start-mining-pearl.bat" "'], P) == "All"
assert mm.mode_of([r"cmd.exe /K C:\M\START-MINING-PEARL-3060-ONLY.BAT", r"cmd.exe /c C:\m\start-mining-pearl.bat"], P) == "One"
assert mm.mode_of(["cmd.exe /c dir"], P) is None

# which cards get reset: not used by the next profile and not already at stock
srb = {0: "01:00.0", 1: "02:00.0"}
assert mm.profile_buses(P[1], ["01:00.0", "02:00.0"], srb) == {"02:00.0"}
assert mm.profile_buses(P[0], ["01:00.0", "02:00.0"], srb) == {"01:00.0", "02:00.0"}
assert mm.buses_to_reset(smi, {"02:00.0"}) == ["01:00.0"]           # 3070 Ti freed and still at 180/290 W
assert mm.buses_to_reset(smi, set()) == ["01:00.0"]                 # 3060 already at stock: skipped
rig100 = [{"bus": f"{i:02x}:00.0", "pl": 100.0, "pl_def": 100.0 if i % 2 else 150.0} for i in range(1, 101)]
assert len(mm.buses_to_reset(rig100, set())) == 50                  # only the 50 still overclocked

# health flags
assert mm.health(smi[0], 0.0, True, 83) == ["0 H/s"]
assert mm.health(dict(smi[0], temp=85.0), 90.0, True, 83) == ["HOT"]
assert mm.health(smi[0], None, False, 83) == ["OC left on"] and mm.health(smi[1], None, False, 83) == []

# auto-switch: in on game start, back 60 s after it closes, only undoes its own switch, manual click cancels
a = mm.AutoSwitch(cfg["auto"])
assert a.step(0, {"explorer.exe"}, "All") is None
assert a.step(5, {"game.exe"}, "All") == "One"
assert a.step(10, {"game.exe"}, "One") is None
assert a.step(20, set(), "One") is None and a.step(79, set(), "One") is None
assert a.step(80, set(), "One") == "All" and not a.active
a = mm.AutoSwitch(cfg["auto"])
assert a.step(0, {"game.exe"}, "All") == "One"
a.cancel()                                                          # user clicked a button
assert a.step(100, set(), "One") is None
a = mm.AutoSwitch(cfg["auto"])
assert a.step(0, {"game.exe"}, "One") is None                      # already there: nothing to undo later
assert a.step(100, set(), "One") is None

# config validation
for bad in ({}, {"miner_dir": "x", "algorithm": "a", "pool": "p", "wallet": "w", "profiles": [{"name": "x"}]},
            {"miner_dir": "x", "algorithm": "a", "pool": "p", "wallet": "w", "profiles": P, "auto": {"profile": "Nope"}}):
    try:
        mm.normalise(bad)
        raise AssertionError(f"accepted bad config {bad}")
    except mm.ConfigError:
        pass
old = mm.normalise({"miner_dir": "x", "algorithm": "a", "pool": "p", "wallet": "w", "both_bat": "b.bat",
                    "single_bat": "s.bat", "single_gpu": 1, "names": {"1": "3060"}})
assert [p["name"] for p in old["profiles"]] == ["Both cards", "3060 only"]   # v1 config still loads
print("all checks passed")
