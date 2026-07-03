"""
Enable / disable auto-launch of DeskGifs on Windows login.

It creates (or removes) a shortcut in your user Startup folder that runs the
widget with pythonw.exe, so it starts silently at login with no console window.

Usage:
    python autostart.py enable
    python autostart.py disable
    python autostart.py status
"""

import os
import subprocess
import sys

SHORTCUT_NAME = "DeskGifs.lnk"


def startup_dir():
    return os.path.join(
        os.environ["APPDATA"],
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )


def shortcut_path():
    return os.path.join(startup_dir(), SHORTCUT_NAME)


def pythonw_exe():
    # sys.executable is python.exe; the windowless twin sits beside it.
    exe = sys.executable
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.exists(candidate) else exe


def enable():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    target = pythonw_exe()
    arg = os.path.join(script_dir, "desk_gif.pyw")
    lnk = shortcut_path()

    ps = (
        "$s = (New-Object -ComObject WScript.Shell)."
        f"CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '\"{arg}\"'; "
        f"$s.WorkingDirectory = '{script_dir}'; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=True,
    )
    print(f"Enabled. Shortcut created at:\n  {lnk}")


def disable():
    lnk = shortcut_path()
    if os.path.exists(lnk):
        os.remove(lnk)
        print(f"Disabled. Removed:\n  {lnk}")
    else:
        print("Already disabled (no shortcut found).")


def status():
    lnk = shortcut_path()
    print("Enabled" if os.path.exists(lnk) else "Disabled")
    print(f"  {lnk}")


def main():
    action = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if action == "enable":
        enable()
    elif action == "disable":
        disable()
    elif action == "status":
        status()
    else:
        sys.exit("Usage: python autostart.py [enable|disable|status]")


if __name__ == "__main__":
    main()
