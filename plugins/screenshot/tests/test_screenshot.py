"""Offline checks for the screenshot plugin: the stdlib PNG decoder (every filter type), the pixel-change
measure behind --diff, the byte-comparison fallback, argument validation, and on macOS the resize
path (sips). Nothing here captures the screen.

    python3 plugins/screenshot/tests/test_screenshot.py      (Windows: py ...)

Stdlib only, Python 3.8+. Exit code 0 when every check passed.
"""
import importlib.util
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
SHOT = PLUGIN / "scripts" / "screenshot.py"
BURST = PLUGIN / "scripts" / "screenshot_burst.py"
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_png(path, w, h, pixels, filters=(0,)):
    """RGB 8-bit PNG; row r uses filter filters[r % len(filters)] (encoded here, so the decoder is tested)."""
    stride = w * 3
    raw, prev = bytearray(), bytearray(stride)
    for r in range(h):
        line = bytearray(pixels[r * stride:(r + 1) * stride])
        ft = filters[r % len(filters)]
        enc = bytearray(stride)
        for x in range(stride):
            a = line[x - 3] if x >= 3 else 0
            b = prev[x]
            c = prev[x - 3] if x >= 3 else 0
            if ft == 0:
                pred = 0
            elif ft == 1:
                pred = a
            elif ft == 2:
                pred = b
            elif ft == 3:
                pred = (a + b) >> 1
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            enc[x] = (line[x] - pred) & 255
        raw += bytes([ft]) + enc
        prev = line

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)


def png_size(path):
    with open(str(path), "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="screenshot-test-"))
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "screenshot" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "screenshot" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs both scripts through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" in skill
          and "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" in skill)

    burst = load(BURST, "screenshot_burst")
    w, h = 7, 5
    px = bytes((x * 37 + y * 11 + c * 5) % 256 for y in range(h) for x in range(w) for c in range(3))
    p = tmp / "all-filters.png"
    write_png(p, w, h, px, filters=(0, 1, 2, 3, 4))
    got = burst.png_pixels(str(p))
    check("png_pixels decodes all five filter types", got == (w, h, 3, px), got and got[:3])
    check("png_pixels refuses non-PNG input", burst.png_pixels(str(BURST)) is None)

    sig_a = ("px", w, h, 3, px)
    changed = bytearray(px)
    changed[0] = (changed[0] + 50) % 256                 # one pixel, one channel, well past the tolerance
    sig_b = ("px", w, h, 3, bytes(changed))
    small = bytearray(px)
    small[3] = (small[3] + burst.CHANNEL_TOLERANCE) % 256   # within the tolerance
    sig_c = ("px", w, h, 3, bytes(small))
    check("changed_fraction: identical frames 0", burst.changed_fraction(sig_a, sig_a) == 0.0)
    check("changed_fraction: one pixel of 35", abs(burst.changed_fraction(sig_b, sig_a) - 1.0 / (w * h)) < 1e-9)
    check("changed_fraction: a change within the tolerance does not count", burst.changed_fraction(sig_c, sig_a) == 0.0)
    check("changed_fraction: different sizes count as all changed", burst.changed_fraction(("px", 1, 1, 3, b"\0\0\0"), sig_a) == 1.0)
    check("changed_fraction: byte fallback", burst.changed_fraction(("md5", "x"), ("md5", "x")) == 0.0
          and burst.changed_fraction(("md5", "x"), ("md5", "y")) == 1.0)
    check("frame_signature without the resize module falls back to bytes",
          burst.frame_signature(str(p), str(tmp / "thumb.png"), None)[0] == "md5")
    check("get_python is the running interpreter", burst.get_python() == sys.executable)

    r = subprocess.run([PY, str(BURST), "--diff-threshold", "1.5", "-n", "1"], capture_output=True, text=True, timeout=60)
    check("burst refuses a threshold outside 0..1 before capturing", r.returncode == 2 and "fraction" in r.stderr, r.stderr[-200:])
    r = subprocess.run([PY, str(SHOT), "--help"], capture_output=True, text=True, timeout=60)
    check("screenshot.py --help", r.returncode == 0 and "--max-size" in r.stdout)

    if platform.system() == "Darwin" and shutil.which("sips"):
        shot = load(SHOT, "screenshot_util")
        big = tmp / "big.png"
        write_png(big, 400, 200, bytes(400 * 200 * 3))
        ok = shot.resize_image(str(big), 100)
        check("resize_image (sips) caps the longest edge", ok and png_size(big) == (100, 50), png_size(big))
        tall = tmp / "tall.png"
        write_png(tall, 60, 300, bytes(60 * 300 * 3))
        ok = shot.resize_image(str(tall), 150)
        check("resize_image keeps a portrait image's aspect", ok and png_size(tall) == (30, 150), png_size(tall))
        same = tmp / "small.png"
        write_png(same, 50, 40, bytes(50 * 40 * 3))
        check("resize_image leaves a small image alone", shot.resize_image(str(same), 100) and png_size(same) == (50, 40))
        sig = burst.frame_signature(str(big), str(tmp / "thumb2.png"), shot)
        check("frame_signature makes a pixel thumbnail with the resize module", sig[0] == "px" and max(sig[1], sig[2]) <= burst.THUMB_EDGE, sig[:3])
    else:
        print("skip resize checks (macOS sips only)")

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0, r.stdout + r.stderr)

    shutil.rmtree(str(tmp), ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
