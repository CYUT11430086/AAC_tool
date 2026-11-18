"""Startup updates for the portable Windows release."""
import hashlib
import json
import http.client
import os
from pathlib import Path
import re
import subprocess
import sys
import atexit
import ctypes as ct
from ctypes import wintypes as wt
from contextlib import contextmanager
import uuid
import winreg
from threading import Event, Thread
from queue import Empty, Queue
from urllib.request import Request, urlopen

VERSION = "1.3.1"
REPO = "CYUT11430086/AAC_tool"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


LEGACY_KEY = r"Software\Cultivation"
SETTINGS_KEY = r"Software\CultivationSettings"
PART_NAME = re.compile(r"\.cultivation-update-[0-9a-f]{32}\.part")


def read_setting(path):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        value, kind = winreg.QueryValueEx(key, "AutoUpdate")
        if kind == winreg.REG_DWORD and value in (0, 1):
            return bool(value)
        raise ValueError("Invalid AutoUpdate value")


def retire_legacy_setting():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LEGACY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, "AutoUpdate")
            except FileNotFoundError:
                pass
            if winreg.QueryInfoKey(key)[:2] != (0, 0):
                return
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, LEGACY_KEY)
    except OSError:
        pass


def auto_update_enabled():
    for path in (SETTINGS_KEY, LEGACY_KEY):
        try:
            return read_setting(path)
        except (OSError, ValueError):
            continue
    return False


def save_auto_update(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "AutoUpdate", 0, winreg.REG_DWORD, int(enabled))
    if read_setting(SETTINGS_KEY) == bool(enabled):
        retire_legacy_setting()


def migrate_setting():
    try:
        old = read_setting(LEGACY_KEY)
        try:
            current = read_setting(SETTINGS_KEY)
        except FileNotFoundError:
            current = old
        save_auto_update(current)
    except (OSError, ValueError):
        pass


@contextmanager
def update_lock(folder):
    # One updater per executable directory. Windows releases this on process death.
    name = hashlib.sha256(os.path.normcase(str(folder.resolve())).encode()).hexdigest()
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    kernel.CreateMutexW.restype = wt.HANDLE
    handle = kernel.CreateMutexW(None, False, "Local\\CultivationUpdate-" + name)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    handle = wt.HANDLE(handle)
    acquired = kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
    try:
        yield acquired
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def cleanup_parts(folder):
    try:
        with update_lock(folder) as acquired:
            if acquired:
                for path in folder.glob(".cultivation-update-*.part"):
                    if PART_NAME.fullmatch(path.name) and path.is_file():
                        path.unlink(missing_ok=True)
    except OSError:
        pass


def initialize_storage():
    folder = Path(sys.executable).resolve().parent
    migrate_setting()
    cleanup_parts(folder)
    atexit.register(cleanup_parts, folder)


def number(version):
    return tuple(map(int, version.split(".")))


def release_asset(release):
    version = release.get("tag_name", "").removeprefix("v")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(SEMVER, version):
        return None
    filename = f"cultivation_{version}.exe"
    url = f"https://github.com/{REPO}/releases/download/{release['tag_name']}/{filename}"
    for asset in release.get("assets", []):
        digest = asset.get("digest") or ""
        if (asset.get("name") == filename and asset.get("browser_download_url") == url
                and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) and asset.get("size", 0) > 0):
            return version, url, digest[7:], asset["size"]
    return None



def latest_update():
    with urlopen(Request(API, headers={"User-Agent": "Cultivation-Updater"}), timeout=5) as response:
        asset = release_asset(json.load(response))
    return asset if asset and number(asset[0]) > number(VERSION) else None


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(asset, current, cancelled, report):
    version, url, digest, size = asset
    target = current.with_name(f"cultivation_{version}.exe")
    if target == current:
        raise ValueError("新版檔名與目前程式相同，請先重新命名目前的程式。")
    if target.exists():
        if file_hash(target) == digest:
            return target
        raise ValueError("資料夾中已有同名檔案，請先移開該檔案。")
    with update_lock(current.parent) as acquired:
        if not acquired:
            raise OSError("另一個程式正在更新，請稍後再試。")
        return download_part(current.parent, target, asset, cancelled, report)


def download_part(folder, target, asset, cancelled, report):
    version, url, digest, size = asset
    staged = folder / f".cultivation-update-{uuid.uuid4().hex}.part"
    try:
        hashed = hashlib.sha256()
        received = 0
        with urlopen(url, timeout=20) as response, staged.open("xb") as stream:
            while chunk := response.read(1024 * 1024):
                if cancelled.is_set():
                    raise InterruptedError()
                received += len(chunk)
                if received > size:
                    raise ValueError("下載大小不符。")
                stream.write(chunk)
                hashed.update(chunk)
                report(received * 100 // size)
        if received != size or hashed.hexdigest() != digest:
            raise ValueError("新版檔案未通過驗證。")
        # Windows rename refuses to overwrite a file created by another updater.
        staged.rename(target)
        return target
    finally:
        staged.unlink(missing_ok=True)


def start_new(target, current, report):
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    report("正在測試新版程式")
    subprocess.run([str(target), "--self-test"], env=env, check=True, timeout=90)
    old_digest = file_hash(current)
    # The new GUI signals readiness after constructing its main window.
    import uuid
    import ctypes as ct
    from ctypes import wintypes as wt
    token = uuid.uuid4().hex
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    kernel.CreateEventW.restype = wt.HANDLE
    handle = kernel.CreateEventW(None, True, False, "Local\\Cultivation-" + token)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    try:
        report("正在啟動新版視窗")
        subprocess.Popen([str(target), "--updated-launch", token], env=env)
        if kernel.WaitForSingleObject(wt.HANDLE(handle), 60000) != 0:
            raise OSError("未收到新版啟動確認，已保留舊版。")
    finally:
        kernel.CloseHandle(wt.HANDLE(handle))
    # Use a literal path and recheck its contents after the old EXE unlocks.
    cleanup = """
$ErrorActionPreference = 'Stop'
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    $path = $env:CULTIVATION_RETIRED_EXE
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { exit }
    try {
        if ((Get-FileHash -LiteralPath $path).Hash -ne $env:CULTIVATION_RETIRED_SHA256) { exit }
        Remove-Item -LiteralPath $path
        exit
    } catch { }
}
"""
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-Command", cleanup],
                     env=dict(env, CULTIVATION_RETIRED_EXE=str(current),
                              CULTIVATION_RETIRED_SHA256=old_digest),
                     creationflags=subprocess.CREATE_NO_WINDOW)


def signal_ready(token):
    import ctypes as ct
    from ctypes import wintypes as wt
    if not re.fullmatch("[0-9a-f]{32}", token):
        return
    kernel = ct.WinDLL("kernel32")
    kernel.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    kernel.OpenEventW.restype = wt.HANDLE
    handle = kernel.OpenEventW(2, False, "Local\\Cultivation-" + token)
    if handle:
        kernel.SetEvent(wt.HANDLE(handle))
        kernel.CloseHandle(wt.HANDLE(handle))


def start_release(root, open_app):
    import tkinter as tk
    from tkinter import ttk
    from time import monotonic
    from math import cos

    messages = Queue()
    cancelled = Event()
    root.title("修仙祕笈")
    bg, ink, muted = "#152126", "#edf5f3", "#a5bab9"
    panel = tk.Frame(root, bg=bg, padx=24, pady=20)
    panel.pack(fill="both", expand=True)
    header = tk.Frame(panel, bg=bg)
    header.pack(fill="x")
    tk.Label(header, text="軟體更新", bg=bg, fg=ink,
             font=("Microsoft JhengHei UI", 15, "bold")).pack(side="left")
    detail = tk.Label(header, text=f"v{VERSION}", bg=bg, fg=muted)
    detail.pack(side="right")
    status = tk.Label(panel, text="正在檢查最新版本", bg=bg, fg=ink, anchor="w",
                      justify="left", wraplength=360, font=("Microsoft JhengHei UI", 10))
    status.pack(fill="x", pady=(14, 4))
    track = tk.Canvas(panel, width=360, height=16, bg=bg, bd=0, highlightthickness=0)
    track.pack(fill="x")
    actions = tk.Frame(panel, bg=bg)
    actions.pack(anchor="w")
    percent = None
    timer = None

    def draw():
        nonlocal timer
        timer = None
        width = max(10, track.winfo_width()-12)
        track.delete("all")
        def segment(left, right, color):
            track.create_line(6+left, 8, 6+right, 8, fill=color, width=8, capstyle="round")
        segment(0, width, "#30454a")
        if percent is None:
            length = width * .26
            left = (width-length) * (1-cos(monotonic()*3.5))/2
            segment(left, left+length, "#329e92")
            segment(left+length*.42, left+length, "#70ddc3")
            timer = root.after(24, draw)
        elif percent:
            segment(0, width*percent/100, "#70ddc3")

    def stop_track():
        nonlocal timer
        if timer is not None:
            root.after_cancel(timer)
            timer = None

    def show_track(value=None):
        nonlocal percent
        stop_track()
        percent = value
        track.pack(fill="x", before=actions)
        draw()

    show_track()

    def proceed():
        cancelled.set()
        stop_track()
        panel.destroy()
        open_app(root)

    def close():
        cancelled.set()
        stop_track()
        root.destroy()

    def check():
        try:
            asset = latest_update()
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            asset = None
        messages.put(("available", asset))

    def install(asset):
        try:
            current = Path(sys.executable).resolve()
            target = download(asset, current, cancelled, lambda percent: messages.put(("percent", percent)))
            if cancelled.is_set():
                return
            start_new(target, current, lambda text: messages.put(("status", text)))
        except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as error:
            messages.put(("error", str(error)))
        else:
            messages.put(("done", None))

    def begin(asset):
        for button in actions.winfo_children():
            button.destroy()
        detail.configure(text=f"v{asset[0]}")
        show_track()
        status.configure(text="正在準備下載新版")
        # Keep the old process alive through verification and handoff.
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        Thread(target=install, args=(asset,), daemon=True).start()

    def poll():
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "available":
                    if value is None:
                        proceed()
                        return
                    if auto_update_enabled():
                        begin(value)
                        continue
                    stop_track()
                    track.pack_forget()
                    status.configure(text=f"有新版本 {value[0]} 可用（目前為 {VERSION}）")
                    ttk.Button(actions, text="更新軟體", command=lambda asset=value: begin(asset)).pack(side="left")
                    ttk.Button(actions, text="稍後", command=proceed).pack(side="left", padx=5)
                elif kind == "percent":
                    show_track(value)
                    status.configure(text=f"正在下載新版（{value}%）")
                elif kind == "status":
                    show_track()
                    status.configure(text=value)
                elif kind == "error":
                    stop_track()
                    track.pack_forget()
                    status.configure(text=f"更新未完成：{value}")
                    root.protocol("WM_DELETE_WINDOW", close)
                    ttk.Button(actions, text="繼續使用目前版本", command=proceed).pack()
                else:
                    stop_track()
                    root.destroy()
                    return
        except Empty:
            if not cancelled.is_set():
                root.after(100, poll)

    root.protocol("WM_DELETE_WINDOW", close)
    Thread(target=check, daemon=True).start()
    poll()
