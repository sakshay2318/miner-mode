"""Self-check of the pure logic, no GPU or miner needed: python test_miner_mode.py"""
import importlib.util
import os

spec = importlib.util.spec_from_file_location("mm", os.path.join(os.path.dirname(os.path.abspath(__file__)), "miner_mode.pyw"))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

cfg = {"both_bat": r"C:\m\start-mining-pearl.bat", "single_bat": r"C:\m\start-mining-pearl-3060-only.bat"}
assert mm.mode_of([r'cmd.exe /c ""C:\m\start-mining-pearl.bat" "'], cfg) == "both"
assert mm.mode_of([r"C:\WINDOWS\system32\cmd.exe /K C:\m\START-MINING-PEARL-3060-ONLY.BAT"], cfg) == "single"  # case and /K
assert mm.mode_of(["cmd.exe /c dir", ""], cfg) is None
assert mm.at_stock([(290.0, 290.0), (112.0, 170.0)], [0]) and not mm.at_stock([(290.0, 290.0), (112.0, 170.0)], [0, 1])
print("all checks passed")
