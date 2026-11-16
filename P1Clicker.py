import ctypes as ct
from ctypes import wintypes as wt
import os
import subprocess
import shutil
import struct
from threading import Event
from io import BytesIO
from tkinter import filedialog
from pathlib import Path
import sys
import random
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from math import ceil
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk

u = ct.windll.user32
try:
    u.SetProcessDpiAwarenessContext(ct.c_void_p(-4))
    u.SetThreadDpiAwarenessContext(ct.c_void_p(-4))
except AttributeError:
    u.SetProcessDPIAware()
callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
u.WindowFromPoint.argtypes = [wt.POINT]
u.WindowFromPoint.restype = wt.HWND
u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
u.GetAncestor.restype = wt.HWND
u.GetForegroundWindow.restype = wt.HWND

def orb_mask(image):
    hsv = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2HSV)
    return cv2.inRange(hsv, (79, 48, 110), (101, 255, 255))


reference = Image.open(Path(__file__).with_name("P1.png")).convert("RGB")
_, labels, stats, _ = cv2.connectedComponentsWithStats(orb_mask(reference))
orb_id = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
rx, ry, rw, rh, _ = stats[orb_id]
silhouette = labels[ry:ry + rh, rx:rx + rw] == orb_id


# The caption below P1 distinguishes the orb from other cyan game effects.
caption = np.asarray(reference.convert("L").crop((63, 190, 168, 220)))


def has_orb_caption(frame, x, y, w, h):
    scale = h / rh
    cx = x + w / 2
    bottom = y + h
    region = frame.crop((int(cx - w * .75), int(bottom - h * .08),
                         int(cx + w * .75), int(bottom + h * .33))).convert("L")
    gray = np.asarray(region)
    for factor in (.9, 1.0, 1.1):
        template = cv2.resize(caption, None, fx=scale * factor, fy=scale * factor)
        if min(template.shape) < 4 or any(a > b for a, b in zip(template.shape, gray.shape)):
            continue
        if cv2.minMaxLoc(cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED))[1] >= .60:
            return True
    return False


def caption_only(frame):
    gray = np.asarray(frame.convert("L"))
    if min(gray.shape) < 24:
        return None
    small = cv2.resize(gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
    best = None
    for scale in (.45, .55, .67, .80, .93, 1.0, 1.15, 1.35, 1.6, 1.9):
        template = cv2.resize(caption, None, fx=scale, fy=scale)
        h, w = template.shape
        if h > gray.shape[0] or w > gray.shape[1]:
            continue
        tiny = cv2.resize(template, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
        _, rough, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(small, tiny, cv2.TM_CCOEFF_NORMED))
        if rough < .55:
            continue
        left, top = max(0, 2*x-4), max(0, 2*y-4)
        region = gray[top:2*y+h+5, left:2*x+w+5]
        _, score, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
        if score >= .60 and (best is None or score > best[2]):
            best = (left+dx+w//2, max(0, top+dy-round(h*1.4)), score)
    return best


def bounds(hwnd):
    rect, origin = wt.RECT(), wt.POINT()
    if not u.GetClientRect(hwnd, ct.byref(rect)) or not u.ClientToScreen(hwnd, ct.byref(origin)):
        return None
    return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom


def match(frame):
    # Use a 1080p analysis canvas and return coordinates in original device pixels.
    width, height = frame.size
    scale = min(1.0, 1920 / width, 1080 / height)
    if scale < 1:
        frame = frame.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             Image.Resampling.BILINEAR)
    mask = orb_mask(frame)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    best = None
    for index in range(1, count):
        x, y, w, h, area = map(int, stats[index])
        if w < 16 or h < 20 or not 0.55 <= w / h <= 1.15 or area < 0.32 * w * h:
            continue
        candidate = (labels[y:y+h, x:x+w] == index).astype(np.uint8)
        aligned = cv2.resize(candidate, (rw, rh), interpolation=cv2.INTER_NEAREST) != 0
        score = np.count_nonzero(aligned & silhouette) / np.count_nonzero(aligned | silhouette)
        if score >= 0.60 and (best is None or score > best[2]) and has_orb_caption(frame, x, y, w, h):
            best = (x + w // 2, y + int(h * 0.78), score)
    if best is None:
        best = caption_only(frame)
        if best is None:
            return None
    x, y, score = best
    return round(x * width / frame.width), round(y * height / frame.height), score


dwm = ct.windll.dwmapi


def taskbar_window(hwnd):
    if not u.IsWindowVisible(hwnd) or hwnd == u.GetShellWindow():
        return False
    cloaked = wt.DWORD()
    dwm.DwmGetWindowAttribute(hwnd, 14, ct.byref(cloaked), ct.sizeof(cloaked))
    style = u.GetWindowLongW(hwnd, -20)
    if cloaked.value or style & 0x80:  # WS_EX_TOOLWINDOW
        return False
    return bool(style & 0x40000 or not (style & 0x08000000 or u.GetWindow(hwnd, 4)))


def windows():
    choices = []

    @callback_type
    def collect(hwnd, _):
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        length = u.GetWindowTextLengthW(hwnd)
        if length and taskbar_window(hwnd) and pid.value != os.getpid():
            title = ct.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, title, length + 1)
            choices.append((title.value, hwnd))
        return True

    u.EnumWindows(collect, 0)
    return choices


PREVIEW_PERIOD = 1 / 30


def find_adb():
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{drive}:/LDPlayer/LDPlayer9/adb.exe")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("adb.exe")


def adb_command(path, *args):
    try:
        result = subprocess.run([path, *args], capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise OSError("ADB 連線逾時") from error
    if result.returncode:
        raise OSError(result.stderr.decode("utf-8", "replace").strip() or "ADB 指令失敗")
    return result.stdout


def wait_for_device(target, cancelled):
    path, serial = target
    delay = 1
    while not cancelled.wait(delay):
        try:
            if serial.startswith(("127.0.0.1:", "localhost:")):
                adb_command(path, "connect", serial)
            if adb_command(path, "-s", serial, "get-state").strip() == b"device":
                return
        except OSError:
            pass
        delay = min(delay * 2, 8)


def device_frame(target):
    path, serial = target
    args = (path, "-s", serial, "exec-out", "screencap")
    raw = adb_command(*args)
    if len(raw) >= 12:
        width, height, fmt = struct.unpack_from("<III", raw)
        offset = len(raw) - width * height * 4
        if fmt in (1, 2) and offset in (12, 16) and width and height:
            return Image.frombytes("RGB", (width, height), raw[offset:], "raw", "RGBX")
    data = adb_command(*args, "-p")
    with Image.open(BytesIO(data)) as frame:
        return frame.convert("RGB")


def shop_template(name, crop):
    image = Image.open(Path(__file__).with_name(name)).convert("L").resize((140, 48))
    return np.asarray(image)[crop]


SHOP_TEXT = (slice(8, 41), slice(26, 114))
UPGRADE_TEXT = (slice(6, 42), slice(27, 62))
SHOP_IMAGES = {
    "store": shop_template("store.png", SHOP_TEXT),
    "upgrade": shop_template("upgrade.png", UPGRADE_TEXT),
}


STORE_FACE = Image.open(Path(__file__).with_name("store.png")).convert("RGB").crop((5, 4, 135, 64))


def locate_store(frame):
    # Match the button face directly, even when its border touches the scenery.
    ratio = min(1.0, 1000 / frame.width)
    small = frame.resize((round(frame.width * ratio), round(frame.height * ratio)), Image.Resampling.BILINEAR)
    rgb = np.asarray(small)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    best, point = .78, None
    for width in range(32, 162, 2):
        height = round(width * STORE_FACE.height / STORE_FACE.width)
        if width > small.width or height > small.height:
            continue
        template = np.asarray(STORE_FACE.resize((width, height), Image.Resampling.BILINEAR))
        scores = cv2.matchTemplate(gray, cv2.cvtColor(template, cv2.COLOR_RGB2GRAY), cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if score <= best:
            continue
        patch = rgb[y:y+height, x:x+width]
        if np.abs(patch.astype(np.int16) - template).mean() < 34:
            best = score
            point = (round((x+width/2)*frame.width/small.width),
                     round((y+height/2)*frame.height/small.height))
    return point


def shop_button(frame, kind):
    rgb = np.asarray(frame)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    store = kind == "store"
    low, high = ((4, 90, 70), (30, 255, 255)) if store else ((38, 85, 65), (90, 255, 255))
    mask = cv2.inRange(hsv, low, high)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, point = .72, None
    crop = SHOP_TEXT if store else UPGRADE_TEXT
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        ratio = w / h
        if h < 12 or not ((1.6 < ratio < 2.7) if store else (3.0 < ratio < 5.2)):
            continue
        # Normalize the colored interior; allow a small margin for border variation.
        patch = cv2.resize(rgb[y:y+h, x:x+w], (140, 48))
        gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
        rows, cols = crop
        search = gray[rows.start-5:rows.stop+5, cols.start-6:cols.stop+6]
        score = cv2.minMaxLoc(cv2.matchTemplate(search, SHOP_IMAGES[kind], cv2.TM_CCOEFF_NORMED))[1]
        if score > best:
            best, point = score, (x + w//2, y + h//2)
    if store and point is None:
        return locate_store(frame)
    return point


def inspect_shop(target, cancelled, stage):
    if not cancelled.is_set():
        frame = device_frame(target)
        upgrade = shop_button(frame, "upgrade") if stage == "inside" else None
        return upgrade, None if upgrade else shop_button(frame, "store")


def device_observation(target, cancelled):
    if not cancelled.is_set():
        frame = device_frame(target)
        return frame.size, match(frame)


def device_tap(target, point, cancelled):
    path, serial = target
    if not cancelled.is_set():
        adb_command(path, "-s", serial, "shell", "input", "tap", *map(str, point))


class ThumbnailProperties(ct.Structure):
    _fields_ = [("flags", wt.DWORD), ("destination", wt.RECT), ("source", wt.RECT),
                ("opacity", wt.BYTE), ("visible", wt.BOOL), ("client_only", wt.BOOL)]


def discover_adb(path):
    lines = adb_command(path, "devices").decode().splitlines()
    windows_by_device = {}
    console = Path(path).with_name("ldconsole.exe")
    if console.is_file():
        try:
            rows = adb_command(str(console), "list2").splitlines()
            for row in rows:
                fields = row.split(b",")
                if len(fields) < 7 or fields[4] != b"1":
                    continue
                serial = adb_command(str(console), "adb", "--index", fields[0].decode(),
                                     "--command", "get-serialno").decode().strip()
                windows_by_device[(path, serial)] = int(fields[2])
        except (ValueError, OSError):
            pass
    return lines, windows_by_device


def capture_preview(hwnd, size):
    try:
        frame = device_frame(hwnd) if isinstance(hwnd, tuple) else ImageGrab.grab(window=hwnd)
        frame.thumbnail(size, Image.Resampling.BILINEAR)
        return frame
    except (OSError, ValueError):
        return None


def apply_window_icons(root, path):
    root.update_idletasks()
    hwnd = wt.HWND(u.GetAncestor(root.winfo_id(), 2))
    dpi = u.GetDpiForWindow(hwnd)
    load = u.LoadImageW
    load.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ct.c_int, ct.c_int, wt.UINT]
    load.restype = wt.HANDLE
    send = u.SendMessageW
    send.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    send.restype = ct.c_ssize_t
    handles = []
    for kind, metric in ((0, 49), (1, 11)):
        size = u.GetSystemMetricsForDpi(metric, dpi)
        handle = load(None, str(path), 1, size, size, 0x10)
        if handle:
            send(hwnd, 0x80, kind, handle)
            handles.append(handle)
    return handles


class App:
    def __init__(self, root):
        self.root = root
        root.title("P1 圖像自動點擊")
        icon = Path(__file__).with_name("P1Clicker.ico")
        root.iconbitmap(str(icon))
        self.window_icons = apply_window_icons(root, icon)
        brand_size = round(40 * root.winfo_fpixels("1i") / 96)
        self.brand = ImageTk.PhotoImage(Image.open(icon.with_suffix(".png")).resize(
            (brand_size, brand_size), Image.Resampling.LANCZOS))
        root.option_add("*Font", "{Microsoft JhengHei UI} 10")
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.dark = tk.BooleanVar(value=True)
        panel = ttk.Frame(root, padding=(20, 16))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1, uniform="controls")
        panel.columnconfigure(1, weight=1, uniform="controls")
        ttk.Label(panel, text="  P1 圖像自動點擊", image=self.brand, compound="left",
                  font=("Microsoft JhengHei UI", 16, "bold")).grid(row=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Checkbutton(panel, text="深色模式", variable=self.dark, command=self.apply_theme).grid(
            row=0, column=1, sticky="e", pady=(0, 16))
        ttk.Label(panel, text="目標視窗").grid(row=1, column=0, sticky="w")
        self.window_glyph = tk.PhotoImage(width=20, height=18)
        self.combo = ttk.Menubutton(panel, image=self.window_glyph, direction="below", compound="left",
                                    style="Picker.TMenubutton", takefocus=True)
        self.menu = tk.Menu(self.combo, tearoff=False, postcommand=self.populate_menu)
        self.combo.configure(menu=self.menu)
        self.adb_path = find_adb()
        self.devices = []
        self.selection = None
        root.minsize(390, 1)
        self.combo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        self.start_button = ttk.Button(panel, text="▶", command=self.start, style="Primary.TButton")
        self.start_button.grid(row=4, column=0, sticky="ew", padx=(0, 5))
        self.pause_button = ttk.Button(panel, text="Ⅱ", style="Secondary.TButton", command=self.pause, state="disabled")
        self.pause_button.grid(row=4, column=1, sticky="ew", padx=(5, 0))
        self.pulse_timers = {}
        for widget in (self.combo, self.start_button, self.pause_button):
            for event in ("<ButtonPress-1>", "<KeyPress-space>", "<KeyPress-Return>"):
                widget.bind(event, self.press_animation, add="+")
        self.status = tk.StringVar()
        ttk.Label(panel, textvariable=self.status).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.shop_enabled = tk.BooleanVar(value=False)
        self.shop_stage = None
        self.shop_owner = None
        self.shop_due = float("inf")
        self.switch_images = []
        for enabled in (False, True):
            icon = Image.new("RGBA", (40, 22))
            draw = ImageDraw.Draw(icon)
            draw.rounded_rectangle((1, 1, 39, 21), radius=10, fill="#087463" if enabled else "#627576")
            x = 21 if enabled else 3
            draw.ellipse((x, 3, x+16, 19), fill="white")
            self.switch_images.append(ImageTk.PhotoImage(icon))
        self.shop_switch = ttk.Checkbutton(panel, text="自動升級商店", compound="left",
                                          variable=self.shop_enabled, command=self.toggle_shop)
        self.shop_switch.configure(image=self.switch_images[0], style="Toolbutton")
        self.shop_switch.grid(row=6, column=0, sticky="w")
        self.shop_switch.grid_remove()
        self.shop_interval = ttk.Combobox(panel, state="readonly", width=16,
                                          values=[f"每 {n} 分鐘檢查" for n in range(1, 6)])
        self.shop_interval.current(4)
        self.shop_interval.grid(row=6, column=1, sticky="e")
        self.shop_interval.grid_remove()
        self.shop_interval.bind("<<ComboboxSelected>>", lambda _: self.toggle_shop())

        self.timer = None
        self.pending = None
        self.anchor = None
        self.choices = []
        self.preview_timer = None
        self.preview_job = None
        self.preview_key = None
        self.cancelled = Event()
        self.discovery_pool = ThreadPoolExecutor(max_workers=1)
        self.discovery = None
        self.adb_windows = {}
        self.thumb = wt.HANDLE()
        self.preview_pool = ThreadPoolExecutor(max_workers=1)
        self.preview_image = None
        self.preview = ttk.Frame(panel, height=195, style="Preview.TFrame")
        self.preview.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.preview_note = ttk.Label(self.preview, text="無可用預覽", style="Preview.TLabel")
        self.preview_note.place(relx=0.5, rely=0.5, anchor="center")
        self.preview.bind("<Configure>", self.update_preview)
        self.preview.bind("<Map>", lambda event: root.after_idle(self.update_preview))
        self.preview.bind("<Unmap>", lambda event: self.release_preview())
        self.apply_theme()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def press_animation(self, event):
        widget = event.widget
        if widget.instate(["disabled"]):
            return
        previous = self.pulse_timers.pop(widget, None)
        if previous is not None:
            self.root.after_cancel(previous)
        original = {self.combo: "Picker.TMenubutton", self.start_button: "Primary.TButton",
                    self.pause_button: "Secondary.TButton"}[widget]
        animated = "Pulse." + original
        widget.configure(style=animated)

        def frame(index):
            # Shift the glyph down and back without changing the control's height.
            offsets = (3, 4, 4, 3, 2, 1, 0)
            if index == len(offsets):
                widget.configure(style=original)
                self.pulse_timers.pop(widget, None)
                return
            offset = offsets[index]
            x, y = (10, 7) if widget is self.combo else (14, 10)
            self.style.configure(animated, padding=(x, y + offset, x, y - offset))
            colors = ("#008a80", "#009c8c", "#11a594", "#129888", "#10897d", "#107b70", "#126b63")
            self.style.map(animated, background=[("", colors[index])], foreground=[("", "#ffffff")])
            self.pulse_timers[widget] = self.root.after(55, frame, index + 1)

        frame(0)

    def apply_theme(self):
        dark = self.dark.get()
        bg, surface, ink, edge = (
            ("#152126", "#24353b", "#edf5f3", "#587075") if dark else
            ("#f2f7f6", "#ffffff", "#18363b", "#99afae")
        )
        accent, on_accent, hover, pressed = (
            ("#70ddc3", "#10362e", "#8be8d1", "#53c7ad") if dark else
            ("#087463", "#ffffff", "#096555", "#094f45")
        )
        self.window_glyph.blank()
        for rect in ((1, 2, 19, 5), (1, 4, 3, 16), (17, 4, 19, 16), (1, 14, 19, 16)):
            self.window_glyph.put(ink, to=rect)
        muted = "#9caeb0" if dark else "#617574"
        inactive = "#2c3c40" if dark else "#dce6e4"
        secondary_hover = "#334a50" if dark else "#e4efec"
        style = self.style
        self.root.configure(background=bg)
        style.configure(".", background=bg, foreground=ink, font=("Microsoft JhengHei UI", 10))
        style.configure("TCheckbutton", background=bg, foreground=ink, focuscolor=accent)
        style.map("TCheckbutton", background=[("active", bg)], indicatorbackground=[
            ("selected", accent), ("!selected", surface)])
        style.configure("Preview.TFrame", background="#172b31")
        style.configure("Preview.TLabel", background="#172b31", foreground="#b5cdcc")
        for name, fill, text, over, down in [
            ("Primary.TButton", accent, on_accent, hover, pressed),
            ("Secondary.TButton", surface, ink, secondary_hover, inactive)
        ]:
            style.configure(name, background=fill, foreground=text, padding=(14, 10), font=("Microsoft JhengHei UI", 10, "bold"),
                            relief="flat", borderwidth=1, bordercolor=edge, focuscolor=text)
            style.map(name, background=[("disabled", inactive), ("pressed", down), ("active", over)],
                      foreground=[("disabled", muted)], bordercolor=[("focus", accent)])
        style.configure("Picker.TMenubutton", background=surface, foreground=ink,
                        anchor="w", width=30, padding=(10, 7), arrowsize=16, arrowcolor=ink, bordercolor=edge)
        style.map("Picker.TMenubutton", background=[("disabled", inactive), ("active", secondary_hover)],
                  bordercolor=[("focus", accent), ("active", accent)])
        self.menu.configure(background=surface, foreground=ink, activebackground=accent,
                            activeforeground=on_accent)
        enabled = wt.BOOL(dark)
        dwm.DwmSetWindowAttribute(wt.HWND(u.GetAncestor(self.root.winfo_id(), 2)), 20,
                                  ct.byref(enabled), ct.sizeof(enabled))

    def selected(self):
        return self.selection

    def choose_window(self, hwnd):
        self.selection = hwnd
        self.update_selection_label()
        self.update_preview()

    def update_selection_label(self):
        if self.shop_owner != self.selection:
            self.shop_owner = self.selection
            self.shop_stage = None
            self.shop_enabled.set(False)
            self.toggle_shop()
        if isinstance(self.selection, tuple):
            self.shop_switch.grid()
            self.shop_interval.grid()
        else:
            self.shop_switch.grid_remove()
            self.shop_interval.grid_remove()
        title = next((name for name, hwnd in self.choices + self.devices if hwnd == self.selection), "")
        self.combo.configure(text=("  " + title) if title else "")

    def populate_menu(self):
        self.refresh()
        self.menu.delete(0, "end")
        for name, hwnd in self.choices:
            self.menu.add_command(label=name, command=lambda h=hwnd: self.choose_window(h))

        self.menu.add_separator()
        self.menu.add_command(label="搜尋雷電 ADB 裝置", command=self.discover_devices)
        for name, target in self.devices:
            self.menu.add_command(label=name, command=lambda t=target: self.choose_window(t))

    def discover_devices(self):
        if not self.adb_path:
            self.adb_path = filedialog.askopenfilename(title="選擇雷電安裝資料夾內的 adb.exe",
                                                       filetypes=[("ADB", "adb.exe")])
        if not self.adb_path:
            return
        if self.discovery is not None:
            return
        self.status.set("搜尋雷電裝置中…")
        self.discovery = self.discovery_pool.submit(discover_adb, self.adb_path)
        self.finish_discovery()

    def finish_discovery(self):
        if not self.discovery.done():
            self.root.after(30, self.finish_discovery)
            return
        job, self.discovery = self.discovery, None
        if self.start_button.instate(["disabled"]):
            return
        try:
            lines, self.adb_windows = job.result()
        except OSError as error:
            self.status.set(str(error))
            return
        self.devices = []
        for line in lines:
            fields = line.split()
            if len(fields) == 2 and fields[1] == "device":
                serial = fields[0]
                if serial.startswith(("emulator-", "127.0.0.1:", "localhost:")):
                    self.devices.append((f"ADB · {serial}", (self.adb_path, serial)))
        if len(self.devices) == 1:
            self.choose_window(self.devices[0][1])
            self.status.set("已選取本機 ADB 裝置。")
        else:
            self.refresh()
            self.status.set("請展開選單選擇 ADB 裝置。" if self.devices else "找不到裝置，請啟動雷電並開啟 ADB 本機連線。")

    def refresh(self):
        self.choices = windows()
        if not any(hwnd == self.selection for _, hwnd in self.choices + self.devices):
            self.selection = self.choices[0][1] if self.choices else None
        self.update_selection_label()
        self.update_preview()

    def native_preview(self):
        src = self.adb_windows.get(self.selected())
        if not src or not u.IsWindow(src):
            return False
        dest = u.GetAncestor(self.root.winfo_id(), 2)
        if dwm.DwmRegisterThumbnail(wt.HWND(dest), wt.HWND(src), ct.byref(self.thumb)):
            return False
        size, area = wt.SIZE(), wt.RECT()
        child = self.preview.winfo_id()
        u.GetClientRect(child, ct.byref(area))
        u.MapWindowPoints(child, dest, ct.byref(area), 2)
        if not dwm.DwmQueryThumbnailSourceSize(self.thumb, ct.byref(size)) and min(size.cx,size.cy)>0:
            w,h = area.right-area.left, area.bottom-area.top
            factor = min(w/size.cx,h/size.cy)
            pw,ph = round(size.cx*factor),round(size.cy*factor)
            x,y = area.left+(w-pw)//2,area.top+(h-ph)//2
            props = ThumbnailProperties()
            props.flags = 1|8|16
            props.visible = props.client_only = True
            props.destination = wt.RECT(x,y,x+pw,y+ph)
            if not dwm.DwmUpdateThumbnailProperties(self.thumb, ct.byref(props)):
                self.preview_note.configure(text="")
                return True
        dwm.DwmUnregisterThumbnail(self.thumb)
        self.thumb = wt.HANDLE()
        return False

    def release_preview(self):
        if self.thumb.value:
            dwm.DwmUnregisterThumbnail(self.thumb)
            self.thumb = wt.HANDLE()
        if self.preview_timer is not None:
            self.root.after_cancel(self.preview_timer)
            self.preview_timer = None
        self.preview_key = None
        self.preview_image = None
        self.preview_note.configure(image="", text="無可用預覽")

    def close(self):
        self.cancelled.set()
        self.release_preview()
        self.discovery_pool.shutdown(wait=False, cancel_futures=True)
        self.preview_pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
        for handle in self.window_icons:
            u.DestroyIcon(wt.HANDLE(handle))

    def update_preview(self, event=None):
        self.release_preview()
        if self.preview.winfo_ismapped() and self.selected() and not self.native_preview():
            self.preview_tick(monotonic())

    def preview_tick(self, deadline):
        self.preview_timer = None
        hwnd = self.selected()
        size = (self.preview.winfo_width(), self.preview.winfo_height())
        key = (hwnd, size)
        if self.preview_job is not None and self.preview_job.done():
            frame = self.preview_job.result()
            self.preview_job = None
            if self.preview_key == key and frame is not None:
                self.preview_image = ImageTk.PhotoImage(frame)
                self.preview_note.configure(image=self.preview_image, text="")
            else:
                self.preview_note.configure(image="", text="無可用預覽")
        if not hwnd or not self.preview.winfo_ismapped() or (not isinstance(hwnd, tuple) and not taskbar_window(hwnd)):
            self.release_preview()
            return
        if self.preview_job is None and min(size) > 1:
            self.preview_key = key
            self.preview_job = self.preview_pool.submit(capture_preview, hwnd, size)
        deadline += PREVIEW_PERIOD
        now = monotonic()
        if deadline < now:
            deadline += ceil((now - deadline) / PREVIEW_PERIOD) * PREVIEW_PERIOD
        delay = max(1, ceil((deadline - now) * 1000))
        self.preview_timer = self.root.after(delay, self.preview_tick, deadline)

    def start(self):
        self.target = self.selected()
        if not self.target or (not isinstance(self.target, tuple) and not taskbar_window(self.target)):
            self.refresh()
            self.status.set("請重新選擇目標視窗。")
            return
        self.release_preview()
        self.preview.grid_remove()
        self.count = 0
        self.combo.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        if not isinstance(self.target, tuple):
            if u.IsIconic(self.target):
                u.ShowWindow(self.target, 9)
            u.SetForegroundWindow(self.target)
        if isinstance(self.target, tuple):
            self.cancelled = Event()
            self.device_missing = 0
            self.device_size = None
            self.device_scan()
        else:
            self.scan()

    def toggle_shop(self):
        enabled = self.shop_enabled.get()
        self.shop_switch.configure(image=self.switch_images[int(enabled)])
        self.shop_due = monotonic() + 60 * (1 + self.shop_interval.current()) if enabled else float("inf")

    def shop_result(self, buttons):
        upgrade, store = buttons
        if self.shop_stage == "open" and not self.shop_enabled.get():
            self.shop_stage = None
            self.schedule_device_scan()
            return
        if self.shop_stage == "inside" and not self.shop_enabled.get():
            self.shop_stage = "close"
        if self.shop_stage == "inside" and not upgrade:
            self.shop_empty += 1
            if self.shop_empty >= 2:
                self.shop_stage = "close"
        if self.shop_stage == "inside":
            point, after = upgrade, "inside"
            if point:
                self.shop_empty = 0
        else:
            point = store
            after = "inside" if self.shop_stage == "open" else None
        if not point:
            if monotonic() >= self.shop_timeout:
                if self.shop_stage == "open":
                    self.shop_finished(None)
                else:
                    self.pause()
                    self.status.set("無法確認商店按鈕，已暫停。請確認商店已關閉再開始。")
                    self.shop_stage = None
                    self.toggle_shop()
                return
            self.timer = self.root.after(400, self.device_scan)
            return
        self.shop_timeout = monotonic() + 12
        self.shop_stage = after
        job = self.preview_pool.submit(device_tap, self.target, point, self.cancelled)
        self.await_device(job, lambda _: self.shop_finished(after), tap=True)

    def shop_finished(self, stage):
        self.shop_stage = stage
        if stage is None:
            self.toggle_shop()
        self.timer = self.root.after(850, self.device_scan)

    def device_scan(self):
        self.sample_started = monotonic()
        if self.shop_stage is None and self.sample_started >= self.shop_due:
            self.shop_stage = "open"
            self.shop_empty = 0
            self.shop_timeout = self.sample_started + 12
        if self.shop_stage:
            self.status.set("正在檢查商店升級…")
            job = self.preview_pool.submit(inspect_shop, self.target, self.cancelled, self.shop_stage)
            self.await_device(job, self.shop_result)
            return
        job = self.preview_pool.submit(device_observation, self.target, self.cancelled)
        self.await_device(job, self.device_result)

    def await_device(self, job, callback, tap=False):
        if self.cancelled.is_set():
            return
        if not job.done():
            self.timer = self.root.after(5, self.await_device, job, callback, tap)
            return
        try:
            result = job.result()
        except (OSError, ValueError, cv2.error) as error:
            if tap and (self.shop_stage or callback != self.device_clicked):
                self.pause()
                self.shop_stage = None
                self.toggle_shop()
                self.status.set("商店點擊結果未確認，請關閉商店後重新開始。")
                return
            self.status.set(f"等待 ADB 裝置重新連線：{self.target[1]}（可暫停）")
            reconnect = self.preview_pool.submit(wait_for_device, self.target, self.cancelled)
            self.await_device(reconnect, self.device_reconnected)
            return
        callback(result)

    def device_reconnected(self, _):
        self.shop_timeout = monotonic() + 12
        self.device_scan()

    def schedule_device_scan(self):
        next_scan = self.sample_started + 0.15
        self.timer = self.root.after(max(1, round(1000 * (next_scan - monotonic()))), self.device_scan)

    def device_result(self, observation):
        size, hit = observation
        if size != self.device_size:
            self.anchor = None
            self.device_size = size
        if hit is None:
            self.device_missing += 1
            if self.device_missing >= 2:
                self.anchor = None
        else:
            self.device_missing = 0
            if self.anchor is None:
                self.anchor = hit[:2]
                job = self.preview_pool.submit(device_tap, self.target, hit[:2], self.cancelled)
                self.await_device(job, self.device_clicked, tap=True)
                return
        self.status.set(f"ADB {size[0]} × {size[1]}，已點擊 {self.count} 次。")
        self.schedule_device_scan()

    def device_clicked(self, result):
        self.count += 1
        self.status.set(f"已點擊 {self.count} 次。")
        self.schedule_device_scan()

    def clear_target(self):
        if self.pending is not None:
            self.root.after_cancel(self.pending)
            self.pending = None
        self.anchor = None

    def pause(self):
        self.cancelled.set()
        self.clear_target()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.combo.configure(state="normal")
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled")
        self.preview.grid()
        self.refresh()
        self.root.after_idle(self.update_preview)
        self.status.set(f"已暫停，這次已點擊 {self.count} 次。")

    def observe(self):
        hwnd = self.target
        if hasattr(u, "SetThreadDpiAwarenessContext"):
            u.SetThreadDpiAwarenessContext(ct.c_void_p(-4))
        if u.GetForegroundWindow() != hwnd or u.IsIconic(hwnd):
            return None, None
        box = bounds(hwnd)
        if not box or box[2] <= box[0] or box[3] <= box[1]:
            return None, None
        frame = ImageGrab.grab(bbox=box, all_screens=True, include_layered_windows=True)
        return box, match(frame)

    def same_target(self, hit):
        return self.anchor is not None and hit is not None and max(
            abs(hit[i] - self.anchor[i]) for i in (0, 1)
        ) <= 24

    def scan(self):
        started = monotonic()
        self.timer = None
        if not u.IsWindow(self.target):
            self.pause()
            self.status.set("目標視窗已關閉。")
            return
        try:
            box, hit = self.observe()
        except (OSError, ValueError, cv2.error) as error:
            self.pause()
            self.status.set(f"擷取失敗：{error}")
            return
        if not self.same_target(hit):
            self.clear_target()
            if hit is not None:
                self.anchor = hit[:2]
                self.pending = self.root.after(random.randint(1500, 2500), self.click, False)
        if box is None:
            self.status.set("等待目標視窗回到前景…")
        else:
            self.status.set(f"辨識中，已點擊 {self.count} 次。")
        self.timer = self.root.after(max(1, round(800 - (monotonic() - started) * 1000)), self.scan)

    def click(self, retry, finished=False):
        self.pending = None
        try:
            box, hit = self.observe()
        except (OSError, ValueError, cv2.error) as error:
            self.pause()
            self.status.set(f"擷取失敗：{error}")
            return
        if not self.same_target(hit):
            self.clear_target()
            return
        x, y, score = hit
        if retry:
            if not finished:
                self.move_retry(box, 1)
                return
            x, y = self.first_point
            x += 10
        point = wt.POINT(box[0] + x, box[1] + y)
        if not self.move_owned(box, point):
            self.clear_target()
            return
        u.mouse_event(2, 0, 0, 0, 0)
        u.mouse_event(4, 0, 0, 0, 0)
        if not retry:
            self.first_point = x, y
        self.count += 1
        if not retry:
            self.pending = self.root.after(500, self.click, True)
        self.status.set(f"已點擊 {self.count} 次，相似度 {score:.0%}。")

    def move_owned(self, box, point):
        hwnd = self.target
        return (bounds(hwnd) == box and u.GetForegroundWindow() == hwnd
                and u.GetAncestor(u.WindowFromPoint(point), 2) == hwnd
                and u.SetCursorPos(point.x, point.y))

    def move_retry(self, box, step):
        self.pending = None
        x, y = self.first_point
        point = wt.POINT(box[0] + x + step, box[1] + y)
        if not self.move_owned(box, point):
            self.clear_target()
        elif step < 10:
            self.pending = self.root.after(12, self.move_retry, box, step + 1)
        else:
            self.click(True, finished=True)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        frame = Image.new("RGB", (460, 340))
        assert match(frame) is None
        frame.paste(reference, (147, 85))
        assert match(frame)[:2] == (147 + rx + rw // 2, 85 + ry + int(rh * 0.78))
        root = tk.Tk()
        root.withdraw()
        App(root)
        root.update()
        root.destroy()
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()





