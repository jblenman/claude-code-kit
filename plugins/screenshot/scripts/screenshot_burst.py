"""Timelapse/burst screenshot capture. No external dependencies.

Captures a series of screenshots at a configurable interval, saving
numbered frames to an output directory. Designed for use with Claude Code's
subagent pattern — capture frames, then selectively analyze only the
interesting ones to minimize token usage.

Usage:
    python screenshot_burst.py                          # 10 frames, 2s apart
    python screenshot_burst.py -n 20 -i 5              # 20 frames, 5s apart
    python screenshot_burst.py -n 30 -i 1 -m 3         # a frame a second, monitor 3
    python screenshot_burst.py -t 60 -i 2              # capture for 60 seconds
    python screenshot_burst.py -w -n 5 -i 2            # the window in front, each frame
    python screenshot_burst.py --diff                   # only save frames that changed
    python screenshot_burst.py --diff-threshold 0.02    # ... by more than 2% of pixels
    python screenshot_burst.py --max-size 1024          # resize frames to 1024px max

Window mode (-w): every frame captures the window that is in front at that
moment; -m is then ignored.
    macOS:  screencapture -l <id of the frontmost normal window>, found through
            CoreGraphics (non-interactive; screenshot.py's own -w waits for a
            click, which a burst cannot use). No window shadow, no shutter sound.
    Windows / Linux: screenshot.py -w (the foreground / active window).

Diff mode (--diff): a frame is kept only when more than --diff-threshold of
its pixels differ from the last kept frame. The threshold is a fraction
(default 0.002 = 0.2%; giving --diff-threshold implies --diff). Frames are
compared as 160-px thumbnails made with screenshot.py's own resize; a pixel
counts as changed when a color channel moves by more than 4 of 255. Measured
on a macOS laptop: a live terminal status line, a blinking cursor and the
clock changed 0.04-0.11% between frames; a window update about 20%. A long
new line of text, a notification or a dialog exceeds the default; a short
line may not (use 0.0005, or 0 to keep any visible change). A frame whose
thumbnail cannot be decoded is compared byte for byte instead (and a note
says so); bytes count any change at all, so a ticking clock defeats them.

Output:
    Creates a timestamped directory with numbered frames:
      screenshots_20260212_213500/
        frame_001.png
        frame_002.png
        ...
        manifest.txt     (lists all frames with timestamps)

    Prints the output directory path to stdout.

Compatible with Python 3.8+. No pip dependencies.
Uses screenshot.py from the same directory for capture and resizing.

Claude Code integration:
    # Capture 10 seconds of activity
    python screenshot_burst.py -t 10 -i 1

    # Then analyze specific frames with subagents:
    Task("Read screenshots_*/frame_005.png and describe what changed")
"""
import argparse
import hashlib
import importlib.util
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import zlib

THUMB_EDGE = 160          # longest edge of the comparison thumbnails (px)
CHANNEL_TOLERANCE = 4     # a pixel "changed" when a channel moves by more than this
DEFAULT_THRESHOLD = 0.002 # measured on macOS: live status line + clock 0.04-0.11%, a window update ~20%


def get_screenshot_script():
    """Find screenshot.py in the same directory as this script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, "screenshot.py")
    if os.path.exists(path):
        return path
    # Fallback: check current directory
    if os.path.exists("screenshot.py"):
        return os.path.abspath("screenshot.py")
    return None


def load_module(path):
    """Import screenshot.py as a module (for its resize_image)."""
    try:
        spec = importlib.util.spec_from_file_location("screenshot_util", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def get_python():
    """The interpreter running this script; it runs screenshot.py too."""
    return sys.executable


def file_hash(filepath):
    """Quick hash of file contents for change detection."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        # Read in chunks for large files
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def mac_front_window_id():
    """Window number of the frontmost normal window (layer 0, visible, at least
    100x100 pt) from CoreGraphics' front-to-back window list, or None."""
    import ctypes
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    except OSError:
        return None
    vp = ctypes.c_void_p

    class CGRect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                    ("w", ctypes.c_double), ("h", ctypes.c_double)]

    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [vp, ctypes.POINTER(CGRect)]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
    cf.CFRelease.argtypes = [vp]
    keys = [vp.in_dll(cg, n).value for n in
            ("kCGWindowLayer", "kCGWindowNumber", "kCGWindowAlpha", "kCGWindowBounds")]
    k_layer, k_number, k_alpha, k_bounds = keys

    def number(d, key, cftype, ctype):
        ref = cf.CFDictionaryGetValue(d, key)
        out = ctype()
        return out.value if ref and cf.CFNumberGetValue(ref, cftype, ctypes.byref(out)) else None

    # kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID
    windows = cg.CGWindowListCopyWindowInfo(1 | 16, 0)
    if not windows:
        return None
    try:
        for i in range(cf.CFArrayGetCount(windows)):
            d = cf.CFArrayGetValueAtIndex(windows, i)
            if number(d, k_layer, 4, ctypes.c_int64) != 0:          # 4 = kCFNumberSInt64Type
                continue
            alpha = number(d, k_alpha, 13, ctypes.c_double)          # 13 = kCFNumberDoubleType
            if alpha is not None and alpha <= 0:
                continue
            rect, bounds = CGRect(), cf.CFDictionaryGetValue(d, k_bounds)
            if not bounds or not cg.CGRectMakeWithDictionaryRepresentation(bounds, ctypes.byref(rect)):
                continue
            if rect.w < 100 or rect.h < 100:
                continue
            return number(d, k_number, 4, ctypes.c_int64)
    finally:
        cf.CFRelease(windows)
    return None


def png_pixels(path, max_pixels=250000):
    """Decode an 8-bit, non-interlaced grey/RGB/grey+alpha/RGBA PNG (stdlib only).
    Returns (width, height, channels, bytes), or None for anything else."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, idat, hdr = 8, [], None
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        if ctype == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", data[pos + 8:pos + 21])
        elif ctype == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + length])
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if not hdr or not idat:
        return None
    w, h, depth, color, _, _, interlace = hdr
    ch = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if depth != 8 or interlace != 0 or ch is None or w * h > max_pixels:
        return None
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    stride = w * ch
    if len(raw) < h * (stride + 1):
        return None
    out, prev, i = bytearray(), bytearray(stride), 0
    for _ in range(h):
        ft, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += stride + 1
        if ft == 1:      # Sub
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 255
        elif ft == 2:    # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ft == 3:    # Average
            for x in range(stride):
                line[x] = (line[x] + (((line[x - ch] if x >= ch else 0) + prev[x]) >> 1)) & 255
        elif ft == 4:    # Paeth
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b, c = prev[x], (prev[x - ch] if x >= ch else 0)
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif ft != 0:
            return None
        out += line
        prev = line
    return w, h, ch, bytes(out)


def frame_signature(frame_path, thumb_path, shot_mod):
    """('px', w, h, channels, bytes) from a THUMB_EDGE thumbnail, else ('md5', digest)."""
    if shot_mod is not None:
        try:
            shutil.copyfile(frame_path, thumb_path)
            if shot_mod.resize_image(thumb_path, THUMB_EDGE):
                px = png_pixels(thumb_path)
                if px is not None:
                    return ("px",) + px
        except Exception:
            pass
        finally:
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
    return ("md5", file_hash(frame_path))


def changed_fraction(sig, ref):
    """Fraction (0..1) of pixels that differ between two signatures."""
    if sig[0] != ref[0] or sig[1:-1] != ref[1:-1]:
        return 1.0          # different kind or size
    if sig[0] == "md5":
        return 0.0 if sig[1] == ref[1] else 1.0
    _, w, h, ch, a = sig
    b = ref[4]
    changed = 0
    for i in range(0, w * h * ch, ch):
        for k in range(i, i + ch):
            if abs(a[k] - b[k]) > CHANNEL_TOLERANCE:
                changed += 1
                break
    return changed / float(w * h)


def capture_frame(python_exe, screenshot_script, output_path, max_size, monitor, window, shot_mod):
    """Capture a single frame. Returns (ok, note)."""
    if window and platform.system() == "Darwin":
        wid = mac_front_window_id()
        if wid is None:
            return False, "no window in front"
        r = subprocess.run(["screencapture", "-x", "-o", "-l%d" % wid, output_path],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(output_path):
            return False, (r.stderr.strip() or "screencapture failed")
        if max_size > 0 and shot_mod is not None and not shot_mod.resize_image(output_path, max_size):
            return True, "resize failed"
        return True, ""
    cmd = [python_exe, screenshot_script, "-o", output_path, "--max-size", str(max_size)]
    if window:
        cmd.append("-w")
    elif monitor is not None:
        cmd.extend(["-m", str(monitor)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    ok = result.returncode == 0 and os.path.exists(output_path)
    return ok, "" if ok else (result.stderr.strip().splitlines() or [""])[-1]


def main():
    parser = argparse.ArgumentParser(
        description="Timelapse/burst screenshot capture",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                         10 frames, 2s apart, all monitors
  %(prog)s -n 5 -i 1 -m 3         5 frames, 1s apart, monitor 3
  %(prog)s -t 30 -i 2             capture for 30 seconds at 2s intervals
  %(prog)s -w -n 5 -i 2           the window in front at each frame
  %(prog)s --diff -t 60 -i 3      60s capture, only save changed frames
  %(prog)s -o ./my_capture         save to specific directory
        """
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-n", "--num-frames", type=int, default=None,
                       help="Number of frames to capture (default: 10)")
    group.add_argument("-t", "--duration", type=int, default=None,
                       help="Duration in seconds (alternative to -n)")
    parser.add_argument("-i", "--interval", type=float, default=2.0,
                        help="Seconds between frames (default: 2.0)")
    parser.add_argument("-m", "--monitor", type=int, default=None,
                        help="Capture specific monitor (0-indexed)")
    parser.add_argument("--max-size", type=int, default=1800,
                        help="Max dimension in pixels (default: 1800)")
    parser.add_argument("--diff", action="store_true",
                        help="Only save frames that differ from the last saved one "
                             "by more than --diff-threshold of their pixels")
    parser.add_argument("--diff-threshold", type=float, default=None, metavar="FRACTION",
                        help="Fraction of pixels that must change, compared on %d-px thumbnails "
                             "(default: %g = %g%%%%; 0 = any visible change; implies --diff)"
                             % (THUMB_EDGE, DEFAULT_THRESHOLD, DEFAULT_THRESHOLD * 100))  # %%%% -> % after argparse's own formatting
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory (default: screenshots_TIMESTAMP in current dir)")
    parser.add_argument("-w", "--window", action="store_true",
                        help="Capture the window in front at each frame (macOS: no click needed); ignores -m")
    args = parser.parse_args()

    diff = args.diff or args.diff_threshold is not None
    threshold = DEFAULT_THRESHOLD if args.diff_threshold is None else args.diff_threshold
    if not 0 <= threshold < 1:
        parser.error("--diff-threshold is a fraction: 0 <= FRACTION < 1")
    if args.window and args.monitor is not None:
        print("NOTE: -w captures the window in front; -m is ignored.", file=sys.stderr)

    # Resolve frame count
    if args.duration is not None:
        num_frames = max(1, int(args.duration / args.interval))
    elif args.num_frames is not None:
        num_frames = args.num_frames
    else:
        num_frames = 10

    # Find screenshot.py
    screenshot_script = get_screenshot_script()
    if not screenshot_script:
        print("ERROR: Cannot find screenshot.py. Place it in the same directory.", file=sys.stderr)
        sys.exit(1)
    shot_mod = load_module(screenshot_script)

    python_exe = get_python()

    # Create output directory
    if args.output_dir:
        out_dir = os.path.abspath(args.output_dir)
    else:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.abspath("screenshots_%s" % timestamp)
    os.makedirs(out_dir, exist_ok=True)

    # Capture loop
    manifest_lines = []
    saved_count = 0
    last_sig = None
    temp_path = os.path.join(out_dir, "_temp_frame.png")
    thumb_path = os.path.join(out_dir, "_thumb.png")
    byte_fallback_noted = False

    print("Capturing %d frames at %.1fs intervals..." % (num_frames, args.interval), file=sys.stderr)
    print("Output: %s" % out_dir, file=sys.stderr)
    if args.window:
        print("Window mode: the window in front at each frame", file=sys.stderr)
    if diff:
        print("Diff mode: saving frames where more than %g%% of pixels changed" % (threshold * 100),
              file=sys.stderr)
    print("", file=sys.stderr)

    start_time = time.time()
    for i in range(num_frames):
        frame_start = time.time()
        frame_num = i + 1
        timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")

        # Capture to temp location first (for diff comparison)
        capture_path = temp_path if diff else os.path.join(out_dir, "frame_%03d.png" % frame_num)

        ok, note = capture_frame(python_exe, screenshot_script, capture_path, args.max_size,
                                 args.monitor, args.window, shot_mod)

        if not ok:
            print("  Frame %d/%d: FAILED%s" % (frame_num, num_frames, " (%s)" % note if note else ""),
                  file=sys.stderr)
            manifest_lines.append("%s  frame_%03d  FAILED  %s" % (timestamp_str, frame_num, note))
        elif diff:
            sig = frame_signature(capture_path, thumb_path, shot_mod)
            if sig[0] == "md5" and not byte_fallback_noted:
                print("  NOTE: thumbnail comparison unavailable; comparing bytes "
                      "(any byte change counts)", file=sys.stderr)
                byte_fallback_noted = True
            frac = None if last_sig is None else changed_fraction(sig, last_sig)
            if frac is not None and frac <= threshold:
                print("  Frame %d/%d: skipped (%.2f%% changed)" % (frame_num, num_frames, frac * 100),
                      file=sys.stderr)
                os.remove(capture_path)
            else:
                saved_count += 1
                final_path = os.path.join(out_dir, "frame_%03d.png" % saved_count)
                os.replace(capture_path, final_path)
                size_kb = os.path.getsize(final_path) / 1024
                change = "first" if frac is None else "changed %.2f%%" % (frac * 100)
                print("  Frame %d/%d -> frame_%03d.png (%.0f KB, %s)" % (
                    frame_num, num_frames, saved_count, size_kb, change), file=sys.stderr)
                manifest_lines.append("%s  frame_%03d.png  %.0fKB  %s" % (
                    timestamp_str, saved_count, size_kb, change))
                last_sig = sig
        else:
            saved_count += 1
            size_kb = os.path.getsize(capture_path) / 1024
            print("  Frame %d/%d: frame_%03d.png (%.0f KB)" % (
                frame_num, num_frames, frame_num, size_kb), file=sys.stderr)
            manifest_lines.append("%s  frame_%03d.png  %.0fKB  %s" % (
                timestamp_str, frame_num, size_kb, note))

        # Wait for next frame (subtract capture time)
        if frame_num < num_frames:
            elapsed = time.time() - frame_start
            wait = max(0, args.interval - elapsed)
            if wait > 0:
                time.sleep(wait)

    # Clean up temp files if they exist
    for p in (temp_path, thumb_path):
        if os.path.exists(p):
            os.remove(p)

    total_time = time.time() - start_time

    # Write manifest
    manifest_path = os.path.join(out_dir, "manifest.txt")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("Screenshot Burst Capture\n")
        f.write("Started: %s\n" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)))
        f.write("Frames captured: %d, Frames saved: %d\n" % (num_frames, saved_count))
        f.write("Interval: %.1fs, Total time: %.1fs\n" % (args.interval, total_time))
        f.write("Max size: %dpx\n" % args.max_size)
        if args.window:
            f.write("Window: the window in front at each frame\n")
        elif args.monitor is not None:
            f.write("Monitor: %d\n" % args.monitor)
        if diff:
            f.write("Diff mode: enabled, threshold %g of pixels (%d-px thumbnails)%s\n" % (
                threshold, THUMB_EDGE, ", byte comparison fallback used" if byte_fallback_noted else ""))
        f.write("\n")
        f.write("Timestamp            Frame           Size    Notes\n")
        f.write("-" * 60 + "\n")
        for line in manifest_lines:
            f.write(line.rstrip() + "\n")

    print("", file=sys.stderr)
    print("Done: %d frames saved in %.1fs" % (saved_count, total_time), file=sys.stderr)

    # Print output directory to stdout (for piping)
    print(out_dir)


if __name__ == "__main__":
    main()
