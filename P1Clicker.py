import ctypes as ct
from ctypes import wintypes as wt
import os
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
from PIL import Image, ImageGrab, ImageTk

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


def bounds(hwnd):
    rect, origin = wt.RECT(), wt.POINT()
    if not u.GetClientRect(hwnd, ct.byref(rect)) or not u.ClientToScreen(hwnd, ct.byref(origin)):
        return None
    return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom


def match(frame):
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
        if score >= 0.60 and (best is None or score > best[2]):
            best = (x + w // 2, y + int(h * 0.78), score)
    return best


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


def capture_preview(hwnd, size):
    try:
        frame = ImageGrab.grab(window=hwnd)
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
        self.dark = tk.BooleanVar(value=False)
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
        self.timer = None
        self.pending = None
        self.anchor = None
        self.choices = []
        self.preview_timer = None
        self.preview_job = None
        self.preview_key = None
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
        title = next((name for name, hwnd in self.choices if hwnd == self.selection), "")
        self.combo.configure(text=("  " + title) if title else "")

    def populate_menu(self):
        self.refresh()
        self.menu.delete(0, "end")
        for name, hwnd in self.choices:
            self.menu.add_command(label=name, command=lambda h=hwnd: self.choose_window(h))

    def refresh(self):
        self.choices = windows()
        if not any(hwnd == self.selection for _, hwnd in self.choices):
            self.selection = self.choices[0][1] if self.choices else None
        self.update_selection_label()
        self.update_preview()

    def release_preview(self):
        if self.preview_timer is not None:
            self.root.after_cancel(self.preview_timer)
            self.preview_timer = None
        self.preview_key = None
        self.preview_image = None
        self.preview_note.configure(image="", text="無可用預覽")

    def close(self):
        self.release_preview()
        self.preview_pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()
        for handle in self.window_icons:
            u.DestroyIcon(wt.HANDLE(handle))

    def update_preview(self, event=None):
        self.release_preview()
        if self.preview.winfo_ismapped() and self.selected():
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
        if not hwnd or not self.preview.winfo_ismapped() or not taskbar_window(hwnd):
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
        if not self.target or not taskbar_window(self.target):
            self.refresh()
            self.status.set("請重新選擇目標視窗。")
            return
        self.release_preview()
        self.preview.grid_remove()
        self.count = 0
        self.combo.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        if u.IsIconic(self.target):
            u.ShowWindow(self.target, 9)
        u.SetForegroundWindow(self.target)
        self.scan()

    def clear_target(self):
        if self.pending is not None:
            self.root.after_cancel(self.pending)
            self.pending = None
        self.anchor = None

    def pause(self):
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
        if not retry:
            self.first_point = x, y
        u.mouse_event(2, 0, 0, 0, 0)
        u.mouse_event(4, 0, 0, 0, 0)
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





