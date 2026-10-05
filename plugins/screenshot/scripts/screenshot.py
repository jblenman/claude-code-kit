"""Cross-platform screenshot utility. No external dependencies.

Uses native OS tools:
  - macOS:   screencapture (built-in)
  - Windows: PowerShell + .NET System.Drawing (built-in)
  - Linux:   gnome-screenshot or import (ImageMagick)

Usage:
    python screenshot.py                     # full screen -> temp file
    python screenshot.py -o ~/shot.png       # full screen -> specific path
    python screenshot.py -r                  # interactive region select
    python screenshot.py -w                  # active window only
    python screenshot.py -d 3               # 3 second delay before capture
    python screenshot.py -m 1               # capture monitor 1 (0-indexed)
    python screenshot.py --max-size 1200     # resize so longest edge <= 1200px

The script prints the output file path to stdout, making it easy
for tools like Claude Code to read the image afterward.

IMPORTANT - Claude Code image context limits:
  Images > 2000x2000 cause API errors, and accumulated image data in a
  session can cause unrecoverable errors (only /clear fixes it). To avoid:
  1. Always use --max-size (default: 1800) to cap resolution
  2. Use a Task subagent to read screenshots instead of reading directly,
     so image data stays out of the main conversation context
  3. Delete temp screenshot files after the subagent describes them

Compatible with Python 3.8+. No pip dependencies.
"""
import argparse
import os
import platform
import subprocess
import sys
import tempfile
import time


def get_default_path():
    """Generate a temp file path for the screenshot."""
    tmp = tempfile.mktemp(suffix=".png", prefix="screenshot_")
    return tmp


def resize_image(filepath, max_size):
    """Resize image so longest edge <= max_size, using native OS tools.
    Preserves aspect ratio. Skips if already within bounds.
    Returns True on success or if no resize needed."""
    system = platform.system()

    if system == "Darwin":
        # Use sips (built-in on macOS)
        # First get current dimensions
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", filepath],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return False
        lines = result.stdout.strip().split("\n")
        width = height = 0
        for line in lines:
            if "pixelWidth" in line:
                width = int(line.split(":")[-1].strip())
            elif "pixelHeight" in line:
                height = int(line.split(":")[-1].strip())
        if max(width, height) <= max_size:
            return True  # already small enough
        # Resize using sips
        if width >= height:
            result = subprocess.run(
                ["sips", "--resampleWidth", str(max_size), filepath],
                capture_output=True, text=True
            )
        else:
            result = subprocess.run(
                ["sips", "--resampleHeight", str(max_size), filepath],
                capture_output=True, text=True
            )
        return result.returncode == 0

    elif system == "Windows":
        # Use PowerShell + .NET System.Drawing
        ps_script = r"""
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('%FILEPATH%')
$w = $img.Width
$h = $img.Height
$maxDim = %MAX_SIZE%
if ($w -le $maxDim -and $h -le $maxDim) {
    $img.Dispose()
    exit 0
}
if ($w -ge $h) {
    $newW = $maxDim
    $newH = [int]($h * $maxDim / $w)
} else {
    $newH = $maxDim
    $newW = [int]($w * $maxDim / $h)
}
$resized = New-Object System.Drawing.Bitmap($newW, $newH)
$graphics = [System.Drawing.Graphics]::FromImage($resized)
$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$graphics.DrawImage($img, 0, 0, $newW, $newH)
$graphics.Dispose()
$img.Dispose()
$resized.Save('%FILEPATH%', [System.Drawing.Imaging.ImageFormat]::Png)
$resized.Dispose()
""".replace("%FILEPATH%", filepath.replace("'", "''")).replace("%MAX_SIZE%", str(max_size))
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True
        )
        return result.returncode == 0

    elif system == "Linux":
        # Try convert (ImageMagick)
        try:
            result = subprocess.run(
                ["convert", filepath, "-resize", "%dx%d>" % (max_size, max_size), filepath],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except FileNotFoundError:
            print("WARNING: ImageMagick not found, skipping resize.", file=sys.stderr)
            return True

    return True


def screenshot_macos(output, region=False, window=False, delay=0, monitor=None):
    """Capture screenshot on macOS using screencapture."""
    cmd = ["screencapture"]

    if region:
        cmd.append("-s")  # interactive region selection
    elif window:
        cmd.append("-w")  # interactive window selection

    if delay > 0:
        cmd.extend(["-T", str(delay)])

    if monitor is not None:
        cmd.extend(["-D", str(monitor + 1)])  # screencapture uses 1-indexed

    cmd.append(output)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR: screencapture failed: %s" % result.stderr.strip(), file=sys.stderr)
        return False
    return True


def screenshot_windows(output, region=False, window=False, delay=0, monitor=None):
    """Capture screenshot on Windows using PowerShell + .NET."""
    if delay > 0:
        time.sleep(delay)

    if region:
        # Use snippingtool for interactive region (Windows 10/11)
        # Falls back to full screen if not available
        print("NOTE: Interactive region select not supported on Windows, capturing full screen.", file=sys.stderr)

    # PowerShell script using .NET System.Drawing
    # Captures all screens by default, or a specific monitor
    ps_script = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$screens = [System.Windows.Forms.Screen]::AllScreens
$monitorIndex = %MONITOR_INDEX%

if ($monitorIndex -ge 0 -and $monitorIndex -lt $screens.Length) {
    $bounds = $screens[$monitorIndex].Bounds
} elseif ($monitorIndex -ge 0) {
    Write-Error "Monitor index $monitorIndex not found. Available: 0..$($screens.Length - 1)"
    exit 1
} else {
    # Capture all screens (virtual desktop)
    $minX = ($screens | ForEach-Object { $_.Bounds.X } | Measure-Object -Minimum).Minimum
    $minY = ($screens | ForEach-Object { $_.Bounds.Y } | Measure-Object -Minimum).Minimum
    $maxX = ($screens | ForEach-Object { $_.Bounds.X + $_.Bounds.Width } | Measure-Object -Maximum).Maximum
    $maxY = ($screens | ForEach-Object { $_.Bounds.Y + $_.Bounds.Height } | Measure-Object -Maximum).Maximum
    $bounds = New-Object System.Drawing.Rectangle($minX, $minY, ($maxX - $minX), ($maxY - $minY))
}

if (%WINDOW_ONLY%) {
    Add-Type @"
    using System;
    using System.Runtime.InteropServices;
    using System.Drawing;
    public class WinAPI {
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    }
"@
    $hwnd = [WinAPI]::GetForegroundWindow()
    $rect = New-Object WinAPI+RECT
    [WinAPI]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
    $bounds = New-Object System.Drawing.Rectangle(
        $rect.Left, $rect.Top,
        ($rect.Right - $rect.Left),
        ($rect.Bottom - $rect.Top)
    )
}

$bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$graphics.Dispose()
$bitmap.Save('%OUTPUT_PATH%', [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
"""
    monitor_idx = monitor if monitor is not None else -1
    ps_script = ps_script.replace("%MONITOR_INDEX%", str(monitor_idx))
    ps_script = ps_script.replace("%WINDOW_ONLY%", "$true" if window else "$false")
    ps_script = ps_script.replace("%OUTPUT_PATH%", output.replace("'", "''"))

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("ERROR: PowerShell screenshot failed: %s" % result.stderr.strip(), file=sys.stderr)
        return False
    return True


def screenshot_linux(output, region=False, window=False, delay=0, monitor=None):
    """Capture screenshot on Linux using available tools."""
    # Try gnome-screenshot first, then import (ImageMagick)
    for tool in ["gnome-screenshot", "import"]:
        try:
            subprocess.run([tool, "--version"], capture_output=True)
        except FileNotFoundError:
            continue

        if tool == "gnome-screenshot":
            cmd = ["gnome-screenshot", "-f", output]
            if region:
                cmd.append("-a")  # area select
            elif window:
                cmd.append("-w")  # active window
            if delay > 0:
                cmd.extend(["-d", str(delay)])
        elif tool == "import":
            if delay > 0:
                time.sleep(delay)
            cmd = ["import"]
            if not region and not window:
                cmd.append("-window")
                cmd.append("root")
            elif window:
                cmd.append("-window")
                # import captures the window you click on by default
            cmd.append(output)

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        print("ERROR: %s failed: %s" % (tool, result.stderr.strip()), file=sys.stderr)
        return False

    print("ERROR: No screenshot tool found. Install gnome-screenshot or ImageMagick.", file=sys.stderr)
    return False


def main():
    parser = argparse.ArgumentParser(description="Cross-platform screenshot utility")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file path (default: temp file)")
    parser.add_argument("-r", "--region", action="store_true",
                        help="Interactive region selection (macOS/Linux)")
    parser.add_argument("-w", "--window", action="store_true",
                        help="Capture active/selected window only")
    parser.add_argument("-d", "--delay", type=int, default=0,
                        help="Delay in seconds before capture")
    parser.add_argument("-m", "--monitor", type=int, default=None,
                        help="Capture specific monitor (0-indexed)")
    parser.add_argument("--max-size", type=int, default=1800,
                        help="Max dimension (longest edge) in pixels (default: 1800). "
                             "Set to 0 to disable resizing.")
    args = parser.parse_args()

    output = args.output or get_default_path()
    # Ensure .png extension
    if not output.lower().endswith(".png"):
        output += ".png"
    # Resolve to absolute path
    output = os.path.abspath(output)

    system = platform.system()
    if system == "Darwin":
        ok = screenshot_macos(output, args.region, args.window, args.delay, args.monitor)
    elif system == "Windows":
        ok = screenshot_windows(output, args.region, args.window, args.delay, args.monitor)
    elif system == "Linux":
        ok = screenshot_linux(output, args.region, args.window, args.delay, args.monitor)
    else:
        print("ERROR: Unsupported platform: %s" % system, file=sys.stderr)
        sys.exit(1)

    if ok and os.path.exists(output):
        if args.max_size > 0:
            if not resize_image(output, args.max_size):
                print("WARNING: Resize failed, using original size.", file=sys.stderr)
        print(output)
    else:
        print("ERROR: Screenshot failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
