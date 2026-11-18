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
from functools import lru_cache
from math import ceil
import tkinter as tk
from tkinter import ttk

from cultivation_update import auto_update_enabled, save_auto_update

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk, ImageFont

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

PICTURES = Path(__file__).with_name("PNGpicture")


def orb_mask(image):
    hsv = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2HSV)
    return cv2.inRange(hsv, (79, 48, 110), (101, 255, 255))


reference = Image.open((PICTURES / "P1.png")).convert("RGB")
_, labels, stats, _ = cv2.connectedComponentsWithStats(orb_mask(reference))
orb_id = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
rx, ry, rw, rh, _ = stats[orb_id]
silhouette = labels[ry:ry + rh, rx:rx + rw] == orb_id


# The caption below P1 distinguishes the orb from other cyan game effects.
caption = np.asarray(reference.convert("L").crop((63, 190, 168, 220)))


game_caption = np.asarray(Image.open((PICTURES / "P2-caption.png")).convert("L"))


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


def caption_only(frame, sample=caption):
    gray = np.asarray(frame.convert("L"))
    if min(gray.shape) < 24:
        return None
    small = cv2.resize(gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
    best = None
    for scale in (.4, .45, .50, .55, .60, .65, .70, .75, .80, .86, .93, 1.0, 1.15, 1.35, 1.6, 1.9):
        template = cv2.resize(sample, None, fx=scale, fy=scale)
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
        best = caption_only(frame, game_caption) or caption_only(frame)
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
    image = Image.open(PICTURES / name).convert("L").resize((140, 48))
    return np.asarray(image)[crop]


SHOP_TEXT = (slice(8, 41), slice(26, 114))
UPGRADE_TEXT = (slice(6, 42), slice(27, 62))
SHOP_IMAGES = {
    "store": shop_template("store.png", SHOP_TEXT),
    "upgrade": shop_template("upgrade.png", UPGRADE_TEXT),
}


STORE_FACE = Image.open((PICTURES / "store.png")).convert("RGB").crop((5, 4, 135, 64))


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


def upgrade_label(rgb, mask):
    # Text is centered with a variable price; search the whole green row.
    # Connected components also find buttons enclosed by other green UI.
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    best, point = .70, None
    template = SHOP_IMAGES["upgrade"]
    for x, y, w, h, area in stats[1:]:
        if h < 10 or not 2.3 <= w/h <= 7 or area < .48*w*h:
            continue
        row = cv2.cvtColor(rgb[y:y+h, x:x+w], cv2.COLOR_RGB2GRAY)
        for factor in (.80, .90, 1.0, 1.10, 1.20):
            scale = h / 48 * factor
            word = cv2.resize(template, None, fx=scale, fy=scale)
            if word.shape[0] > h or word.shape[1] > w:
                continue
            score = cv2.minMaxLoc(cv2.matchTemplate(row, word, cv2.TM_CCOEFF_NORMED))[1]
            if score > best:
                best, point = score, (int(x+w//2), int(y+h//2))
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
    if point is None:
        return locate_store(frame) if store else upgrade_label(rgb, mask)
    return point


DJ_BANNER = Image.open((PICTURES / "Dj_up.png")).convert("RGB")
DJ_ICON = np.asarray(DJ_BANNER.crop((5, 6, 93, 92)).convert("L"))
DJ_ROW = np.asarray(Image.open((PICTURES / "Dj_P.png")).convert("RGB"))


def light_letters(rgb):
    rgb = rgb.astype(np.int16)
    return ((rgb.min(axis=2) > 155) & (np.ptp(rgb, axis=2) < 40)).astype(np.uint8)


def normalized_digit(mask):
    x, y, w, h = cv2.boundingRect(mask)
    return cv2.resize(mask[y:y+h, x:x+w].astype(np.float32), (22, 30))


@lru_cache(maxsize=1)
def digit_samples():
    samples = [[] for _ in range(10)]
    for name in ("arialbd.ttf", "segoeuib.ttf", "arial.ttf", "calibrib.ttf"):
        try:
            font = ImageFont.truetype(name, 40)
        except OSError:
            continue
        for digit, group in enumerate(samples):
            glyph = Image.new("L", (64, 64))
            ImageDraw.Draw(glyph).text((4, 0), str(digit), fill=255, font=font)
            group.append(normalized_digit((np.asarray(glyph) > 120).astype(np.uint8)))
    for digit, left, right in ((3, 97, 112), (4, 115, 133)):
        samples[digit].append(normalized_digit(light_letters(DJ_ROW[10:31, left:right])))
    return samples


def tribulation_rate(row):
    # Align the fixed success-rate caption and percent sign around the full number.
    mask = light_letters(row)
    sample = light_letters(DJ_ROW)
    label = sample[9:34, 13:87]
    percent = sample[10:31, 135:158]
    _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(mask, label, cv2.TM_CCOEFF_NORMED))
    if score < .70:
        return None
    start = x + label.shape[1]
    band = mask[max(0, y-2):y+26, start:start+102]
    if band.shape[0] < 21 or band.shape[1] < 23:
        return None
    _, score, _, (end, top) = cv2.minMaxLoc(cv2.matchTemplate(band, percent, cv2.TM_CCOEFF_NORMED))
    if score < .72 or not end:
        return None
    digits = band[top:top+21, :end]
    # Isolated highlights between the caption and number are common on bright scenery.
    _, components, stats, _ = cv2.connectedComponentsWithStats(digits)
    keep = stats[:, cv2.CC_STAT_AREA] >= 8
    keep[0] = False
    digits = keep[components].astype(np.uint8)
    runs = np.flatnonzero(np.diff(np.r_[0, digits.any(axis=0), 0])).reshape(-1, 2)
    if not 1 <= len(runs) <= 3:
        return None
    value = 0
    for left, right in runs:
        glyph = digits[:, left:right]
        if not 5 <= right-left <= 22 or cv2.boundingRect(glyph)[3] < 18:
            return None
        glyph = normalized_digit(glyph)
        errors = [min((np.mean(np.abs(glyph-ref)) for ref in group), default=1)
                  for group in digit_samples()]
        first, second = np.argsort(errors)[:2]
        if errors[first] > .27 or errors[second]-errors[first] < .035:
            return None
        value = 10 * value + int(first)
    return value if value <= 100 else None


def inspect_tribulation(target, cancelled):
    if cancelled.is_set():
        return None
    frame = device_frame(target)
    gray = np.asarray(frame.convert("L"))
    best = .78
    point = None
    for scale in np.geomspace(.4, 2, 34):
        if cancelled.is_set():
            return None
        icon = cv2.resize(DJ_ICON, None, fx=scale, fy=scale)
        if any(a > b for a, b in zip(icon.shape, gray.shape)):
            continue
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(gray, icon, cv2.TM_CCOEFF_NORMED))
        if score > best:
            best = score
            point = (x-5*scale, y-6*scale, scale)
    if point:
        x, y, scale = point
        box = tuple(round(v) for v in (x+95*scale, y+49*scale, x+425*scale, y+92*scale))
        row = np.asarray(frame.crop(box).resize((330, 43), Image.Resampling.BILINEAR))
        rate = tribulation_rate(row)
        if rate is not None:
            return rate, (round(x+DJ_BANNER.width*scale/2), round(y+DJ_BANNER.height*scale/2))


DJ_DIALOG_TITLE = np.asarray(Image.open((PICTURES / "Dj_2up.png"))
                             .convert("L").crop((222, 7, 352, 55)))
DJ_CONFIRM_FACE = np.asarray(Image.open((PICTURES / "Dj_2_c.png"))
                             .convert("L").crop((7, 6, 264, 77)))


def find_dj_confirm(frame):
    rgb = np.asarray(frame)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    green = cv2.inRange(hsv, (55, 90, 55), (90, 255, 255))
    contours, _ = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        x, y, w, h = cv2.boundingRect(contour)
        if h < 18 or not 3.2 < w/h < 4.1:
            continue
        face = cv2.resize(gray[y:y+h, x:x+w], (257, 71))
        if cv2.matchTemplate(face, DJ_CONFIRM_FACE, cv2.TM_CCOEFF_NORMED).item() < .78:
            continue
        # Search for the fixed dialog heading above this particular button.
        scale = w / 257
        title = cv2.resize(DJ_DIALOG_TITLE, None, fx=scale, fy=scale)
        region = gray[max(0, y-round(225*scale)):max(0, y-round(95*scale)),
                      max(0, x+round(150*scale)):x+round(395*scale)]
        if any(a < b for a, b in zip(region.shape, title.shape)):
            continue
        if cv2.minMaxLoc(cv2.matchTemplate(region, title, cv2.TM_CCOEFF_NORMED))[1] >= .75:
            return x+w//2, y+h//2


def inspect_dj_confirm(target, cancelled):
    if not cancelled.is_set():
        return find_dj_confirm(device_frame(target))


def inspect_shop(target, cancelled, stage):
    if not cancelled.is_set():
        frame = device_frame(target)
        upgrade = shop_button(frame, "upgrade") if stage == "inside" else None
        return upgrade, None if upgrade else shop_button(frame, "store")


@lru_cache(maxsize=3)
def sword_template(name):
    with Image.open(PICTURES / (name + ".png")) as image:
        return np.asarray(image.convert("RGB"))


def sword_button(frame, name, region=None):
    # The references are cropped screenshots. Search at several button sizes.
    box = region or (0, 0, frame.width, frame.height)
    area = frame.crop(box)
    ratio = min(1, 800 / frame.width)
    rgb = np.asarray(area.resize((max(1, round(area.width * ratio)),
                                  max(1, round(area.height * ratio)))))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    source = sword_template(name)
    best, found = .84, None
    widths = [source.shape[1] * ratio, source.shape[1] * frame.width / 1808 * ratio] + list(
        np.geomspace(10, min(600, rgb.shape[1]), 120))
    for width in widths:
        w = round(width)
        h = max(1, round(w * source.shape[0] / source.shape[1]))
        if w > rgb.shape[1] or h > rgb.shape[0]:
            continue
        template = cv2.resize(source, (w, h), interpolation=cv2.INTER_AREA)
        scores = cv2.matchTemplate(gray, cv2.cvtColor(template, cv2.COLOR_RGB2GRAY),
                                   cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(scores)
        if score > best and np.abs(rgb[y:y+h, x:x+w].astype(np.int16) - template).mean() < 32:
            best = score
            found = (box[0] + round((x+w/2)/ratio), box[1] + round((y+h/2)/ratio))
    return found


def check_sword(target, cancelled, stopped):
    frame = device_frame(target)
    entry = sword_button(frame, "swort_in_bt")
    if not entry or cancelled.is_set() or stopped.is_set():
        return True
    device_tap(target, entry, cancelled)
    for _ in range(8):
        if cancelled.wait(.4):
            return True
        frame = device_frame(target)
        close = sword_button(frame, "sword_exit")
        if close:
            break
    else:
        return False
    upgrade = sword_button(frame, "sword_up_bt", (0, close[1], frame.width//2, frame.height))
    if upgrade and not stopped.is_set():
        device_tap(target, upgrade, cancelled)
        if cancelled.wait(.65):
            return True
        close = sword_button(device_frame(target), "sword_exit")
    if not close:
        return False
    device_tap(target, close, cancelled)
    for _ in range(5):
        if cancelled.wait(.4) or not sword_button(device_frame(target), "sword_exit"):
            return True
    return False


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


class TimeWheel(tk.Canvas):
    row_height = 22

    def __init__(self, parent, value, unit, command):
        super().__init__(parent, width=72, height=66, takefocus=True,
                         highlightthickness=0, borderwidth=0)
        self.command = command
        self.unit = unit
        self.position = self.goal = value
        self.animation = None
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<MouseWheel>", lambda e: self.select(self.goal - (1 if e.delta > 0 else -1)))
        for key, step in (("Up", -1), ("Down", 1), ("Prior", -10), ("Next", 10)):
            self.bind("<" + key + ">", lambda e, step=step: self.select(self.goal + step))

    def theme(self, bg, surface, ink, muted):
        self.colors = surface, ink, muted
        self.configure(background=bg)
        self.paint()

    def paint(self):
        self.delete("all")
        surface, ink, muted = self.colors
        self.create_rectangle(2, 22, 71, 46, fill=surface, outline="")
        center = round(self.position)
        for n in range(max(0, center-2), min(59, center+2)+1):
            selected = n == center
            self.create_text(37, 34+(n-self.position)*self.row_height,
                             text=f"{n} {self.unit}" if selected else str(n),
                             font=("Microsoft JhengHei UI", 11 if selected else 9,
                                   "bold" if selected else "normal"),
                             fill=ink if selected else muted)

    def stop_animation(self):
        if self.animation is not None:
            self.after_cancel(self.animation)
            self.animation = None

    def press(self, event):
        self.focus_set()
        self.stop_animation()
        self.start_y = event.y
        self.origin = self.position

    def drag(self, event):
        self.position = max(0, min(59, self.origin + (self.start_y-event.y)/self.row_height))
        self.paint()

    def release(self, event):
        self.drag(event)
        offset = round((event.y-34)/self.row_height) if abs(event.y-self.start_y)<4 else 0
        self.select(round(self.position)+offset)

    def select(self, value):
        self.stop_animation()
        self.goal = max(0, min(59, value))
        self.snap()
        self.command()

    def snap(self):
        distance = self.goal-self.position
        self.animation = None
        if abs(distance) < .025:
            self.position = self.goal
        else:
            self.position += distance * .35
            self.animation = self.after(16, self.snap)
        self.paint()


class App:
    sword_interval_seconds = 30

    def __init__(self, root):
        self.root = root
        root.title("修仙祕笈")
        icon = Path(__file__).with_name("P1Clicker.ico")
        root.iconbitmap(str(icon))
        self.window_icons = apply_window_icons(root, icon)
        brand_size = round(40 * root.winfo_fpixels("1i") / 96)
        with Image.open(icon) as artwork:
            size = min(artwork.ico.sizes(), key=lambda pair: abs(pair[0] - brand_size))
            self.brand = ImageTk.PhotoImage(artwork.ico.getimage(size).resize(
                (brand_size, brand_size), Image.Resampling.LANCZOS))
        root.option_add("*Font", "{Microsoft JhengHei UI} 10")
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.dark = tk.BooleanVar(value=True)
        panel = ttk.Frame(root, padding=(20, 16))
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1, uniform="controls")
        panel.columnconfigure(1, weight=1, uniform="controls")
        ttk.Label(panel, text="  修仙祕笈", image=self.brand, compound="left",
                  font=("Microsoft JhengHei UI", 16, "bold")).grid(row=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Checkbutton(panel, text="深色模式", variable=self.dark, command=self.apply_theme).grid(
            row=0, column=1, sticky="e", pady=(0, 16))
        ttk.Label(panel, text="目標視窗").grid(row=1, column=0, sticky="w")
        self.auto_update = tk.BooleanVar(value=auto_update_enabled())
        ttk.Checkbutton(panel, text="自動更新軟體", variable=self.auto_update,
                        command=self.change_auto_update).grid(row=1, column=1, sticky="e")
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
        self.switch_positions = {}
        self.switch_timers = {}
        for step in range(9):
            icon = Image.new("RGBA", (40, 22))
            draw = ImageDraw.Draw(icon)
            draw.rounded_rectangle((1, 1, 39, 21), radius=10, fill=tuple(round(a + (b-a)*step/8) for a, b in zip((98,117,118), (8,116,99))))
            x = 3 + round(18*step/8)
            draw.ellipse((x, 3, x+16, 19), fill="white")
            self.switch_images.append(ImageTk.PhotoImage(icon))
        self.shop_switch = ttk.Checkbutton(panel, text="自動升級商店", compound="left",
                                          variable=self.shop_enabled, command=self.toggle_shop)
        self.shop_switch.configure(image=self.switch_images[0], style="Switch.Toolbutton")
        self.shop_switch.grid(row=6, column=0, sticky="w")
        self.shop_switch.grid_remove()
        self.shop_interval = ttk.Combobox(panel, state="readonly", width=16,
                                          values=[f"每 {n} 分鐘檢查" for n in range(1, 6)])
        self.shop_interval.current(4)
        self.shop_interval.grid(row=6, column=1, sticky="e")
        self.shop_interval.grid_remove()
        self.shop_interval.bind("<<ComboboxSelected>>", lambda _: self.toggle_shop())

        self.dj_enabled = tk.BooleanVar(value=False)
        self.dj_due = float("inf")
        self.dj_switch = ttk.Checkbutton(panel, text="自動渡劫", compound="left",
                                         variable=self.dj_enabled, command=self.toggle_dj,
                                         image=self.switch_images[0], style="Switch.Toolbutton")
        self.adb_separator = ttk.Separator(panel, orient="horizontal")
        self.adb_separator.grid(row=7, columnspan=2, sticky="ew", pady=6)
        self.adb_separator.grid_remove()
        self.dj_switch.grid(row=8, column=0, sticky="w")
        self.dj_switch.grid_remove()

        self.dj_options = ttk.Frame(panel)
        self.dj_options.grid(row=8, column=1, sticky="e")
        self.dj_options.grid_remove()
        self.dj_threshold = ttk.Combobox(self.dj_options, state="readonly", width=11,
            values=[f"高於 {n}%" for n in range(50, 100, 10)] + ["僅限 100%"])
        self.dj_threshold.current(2)
        self.dj_threshold.pack(anchor="e")
        self.dj_interval = ttk.Combobox(self.dj_options, state="readonly", width=16,
            values=self.shop_interval.cget("values"))
        self.dj_interval.current(0)
        self.dj_interval.pack(anchor="e", pady=(3, 0))
        self.dj_interval.bind("<<ComboboxSelected>>", lambda _: self.toggle_dj())

        self.sword_separator = ttk.Separator(panel, orient="horizontal")
        self.sword_separator.grid(row=9, columnspan=2, sticky="ew", pady=6)
        self.sword_separator.grid_remove()
        self.upgrade_progress = ttk.Progressbar(panel, mode="indeterminate")
        self.upgrade_progress.grid(row=11, columnspan=2, sticky="ew", pady=(6, 0))
        self.upgrade_progress.grid_remove()
        self.sword_enabled = tk.BooleanVar(value=True)
        self.sword_stop = Event()
        self.sword_switch = ttk.Checkbutton(panel, text="自動升級寶劍", compound="left",
            variable=self.sword_enabled, command=self.toggle_sword,
            image=self.switch_images[-1], style="Switch.Toolbutton")
        self.switch_positions[self.sword_switch] = len(self.switch_images)-1
        self.sword_switch.grid(row=10, column=0, sticky="w", pady=(6, 0))
        self.sword_wheels = ttk.Frame(panel)
        self.sword_wheels.grid(row=10, column=1, sticky="e", pady=(6, 0))
        minutes, seconds = divmod(self.sword_interval_seconds, 60)
        self.minute_wheel = TimeWheel(self.sword_wheels, minutes, "分", self.set_sword_interval)
        self.second_wheel = TimeWheel(self.sword_wheels, seconds, "秒", self.set_sword_interval)
        self.minute_wheel.pack(side="left")
        self.second_wheel.pack(side="left")
        self.sword_switch.grid_remove()
        self.sword_wheels.grid_remove()

        self.timer = None
        self.release_timer = None
        self.pending = None
        self.anchor = None
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
        for wheel in (self.minute_wheel, self.second_wheel):
            wheel.theme(bg, surface, ink, muted)
        style = self.style
        self.root.configure(background=bg)
        style.configure(".", background=bg, foreground=ink, font=("Microsoft JhengHei UI", 10))
        style.layout("Switch.Toolbutton", [("Toolbutton.padding", {"sticky": "nswe",
                     "children": [("Toolbutton.label", {"sticky": "nswe"})]})])
        style.configure("Horizontal.TProgressbar", background=accent, troughcolor=surface)
        style.configure("Switch.Toolbutton", background=bg, foreground=ink,
                        relief="flat", borderwidth=0, focuscolor=accent)
        style.map("Switch.Toolbutton", background=[("", bg)], foreground=[("", ink)],
                  relief=[("", "flat")], bordercolor=[("", bg)],
                  lightcolor=[("", bg)], darkcolor=[("", bg)])
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
        style.configure("TCombobox", foreground=ink, fieldbackground=surface,
                        background=surface, arrowcolor=ink, bordercolor=edge)
        style.map("TCombobox",
                  fieldbackground=[("readonly", surface)],
                  foreground=[("readonly", ink)],
                  selectbackground=[("readonly", surface)],
                  selectforeground=[("readonly", ink)],
                  background=[("active", secondary_hover), ("readonly", surface)],
                  arrowcolor=[("readonly", ink)], bordercolor=[("focus", accent)])
        # The popup list is a classic Tk listbox, independent of the ttk field.
        for combo in (self.shop_interval, self.dj_threshold, self.dj_interval):
            popup = combo.tk.call("ttk::combobox::PopdownWindow", combo)
            combo.tk.call(f"{popup}.f.l", "configure", "-background", surface,
                          "-foreground", ink, "-selectbackground", accent,
                          "-selectforeground", on_accent)
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
            self.dj_enabled.set(False)
            self.toggle_dj()
            self.shop_owner = self.selection
            self.shop_stage = None
            self.shop_enabled.set(False)
            self.toggle_shop()
        if isinstance(self.selection, tuple):
            self.adb_separator.grid()
            self.sword_separator.grid()
            self.sword_switch.grid()
            self.sword_wheels.grid()
            self.dj_options.grid()
            self.dj_switch.grid()
            self.shop_switch.grid()
            self.shop_interval.grid()
        else:
            self.adb_separator.grid_remove()
            self.sword_separator.grid_remove()
            self.sword_switch.grid_remove()
            self.sword_wheels.grid_remove()
            self.dj_options.grid_remove()
            self.dj_switch.grid_remove()
            self.shop_switch.grid_remove()
            self.shop_interval.grid_remove()
        title = next((name for name, hwnd in self.devices if hwnd == self.selection), "")
        self.combo.configure(text="  " + (title or "搜尋雷電 ADB 裝置"))

    def populate_menu(self):
        self.refresh()
        self.menu.delete(0, "end")
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
        if not any(hwnd == self.selection for _, hwnd in self.devices):
            self.selection = None
        self.update_selection_label()
        self.update_preview()

    def native_preview(self):
        target = self.selected()
        src = self.adb_windows.get(target) if isinstance(target, tuple) else target
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
        self.release_mouse()
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
            self.status.set("請先搜尋並選取 ADB 裝置。")
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
            self.root.update_idletasks()
            u.SetForegroundWindow(self.target)
        if isinstance(self.target, tuple):
            self.cancelled = Event()
            self.reset_sword_timer()
            self.device_missing = 0
            self.device_size = None
            self.device_scan()
        else:
            self.scan()

    def animate_switch(self, widget, enabled):
        timer = self.switch_timers.pop(widget, None)
        if timer is not None:
            self.root.after_cancel(timer)
        position = self.switch_positions.get(widget, 0)
        goal = len(self.switch_images)-1 if enabled else 0
        if position != goal:
            position += 1 if enabled else -1
            self.switch_positions[widget] = position
            widget.configure(image=self.switch_images[position])
            if position != goal:
                self.switch_timers[widget] = self.root.after(16, self.animate_switch, widget, enabled)

    def toggle_dj(self):
        enabled = self.dj_enabled.get()
        self.animate_switch(self.dj_switch, enabled)
        self.dj_due = monotonic() + 60 * (1 + self.dj_interval.current()) if enabled else float("inf")

    def dj_result(self, observation):
        threshold = 50 + 10 * self.dj_threshold.current()
        if observation and self.dj_enabled.get() and observation[0] >= min(threshold + 1, 100):
            _, point = observation
            job = self.preview_pool.submit(device_tap, self.target, point, self.cancelled)
            self.await_device(job, self.dj_opened, tap=True)
        else:
            self.schedule_device_scan()

    def dj_opened(self, _):
        self.count += 1
        self.dj_confirm_until = monotonic() + 5
        self.check_dj_confirm()

    def check_dj_confirm(self):
        if not self.dj_enabled.get() or monotonic() >= self.dj_confirm_until:
            self.schedule_device_scan()
            return
        self.status.set("正在確認渡劫視窗…")
        job = self.preview_pool.submit(inspect_dj_confirm, self.target, self.cancelled)
        self.await_device(job, self.dj_confirm_result)

    def dj_confirm_result(self, point):
        if not self.dj_enabled.get():
            self.schedule_device_scan()
        elif point:
            job = self.preview_pool.submit(device_tap, self.target, point, self.cancelled)
            self.await_device(job, self.device_clicked, tap=True)
        else:
            self.timer = self.root.after(250, self.check_dj_confirm)

    def reset_sword_timer(self):
        self.sword_due = (monotonic() + self.sword_interval_seconds
                          if self.sword_enabled.get() else float("inf"))

    def toggle_sword(self):
        enabled = self.sword_enabled.get()
        if enabled:
            self.sword_stop = Event()
        else:
            self.sword_stop.set()
        self.animate_switch(self.sword_switch, enabled)
        self.reset_sword_timer()

    def set_sword_interval(self):
        seconds = 60 * self.minute_wheel.goal + self.second_wheel.goal
        if seconds == 0:
            self.second_wheel.select(1)
            return
        self.sword_interval_seconds = seconds
        self.reset_sword_timer()

    def change_auto_update(self):
        try:
            save_auto_update(self.auto_update.get())
        except OSError:
            self.auto_update.set(auto_update_enabled())
            self.status.set("無法儲存自動更新設定。")

    def show_upgrade_progress(self):
        self.upgrade_progress.grid()
        self.upgrade_progress.start(18)

    def hide_upgrade_progress(self):
        self.upgrade_progress.stop()
        self.upgrade_progress.grid_remove()

    def toggle_shop(self):
        enabled = self.shop_enabled.get()
        self.animate_switch(self.shop_switch, enabled)
        self.shop_due = monotonic() + 60 * (1 + self.shop_interval.current()) if enabled else float("inf")

    def shop_result(self, buttons):
        upgrade, store = buttons
        if self.shop_stage == "open" and not self.shop_enabled.get():
            self.shop_stage = None
            self.hide_upgrade_progress()
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
            self.hide_upgrade_progress()
            self.toggle_shop()
        self.timer = self.root.after(850, self.device_scan)

    def device_scan(self):
        self.sample_started = monotonic()
        if self.shop_stage is None and self.sample_started >= self.shop_due:
            self.shop_stage = "open"
            self.shop_empty = 0
            self.shop_timeout = self.sample_started + 12
        if self.shop_stage:
            self.show_upgrade_progress()
            self.status.set("正在檢查商店升級…")
            job = self.preview_pool.submit(inspect_shop, self.target, self.cancelled, self.shop_stage)
            self.await_device(job, self.shop_result)
            return
        if self.sample_started >= self.sword_due:
            self.sword_due = float("inf")
            self.show_upgrade_progress()
            self.status.set("正在檢查寶劍升級…")
            job = self.preview_pool.submit(check_sword, self.target, self.cancelled, self.sword_stop)
            self.await_device(job, self.sword_finished)
            return
        if self.sample_started >= self.dj_due:
            self.dj_due = self.sample_started + 60 * (1 + self.dj_interval.current())
            self.status.set("正在檢查渡劫成功率…")
            job = self.preview_pool.submit(inspect_tribulation, self.target, self.cancelled)
            self.await_device(job, self.dj_result)
            return
        job = self.preview_pool.submit(device_observation, self.target, self.cancelled)
        self.await_device(job, self.device_result)

    def sword_finished(self, closed):
        self.hide_upgrade_progress()
        self.reset_sword_timer()
        if not closed:
            self.status.set("未確認寶劍介面，繼續偵測靈氣。")
        self.schedule_device_scan()

    def await_device(self, job, callback, tap=False):
        if self.cancelled.is_set():
            return
        if not job.done():
            self.timer = self.root.after(5, self.await_device, job, callback, tap)
            return
        try:
            result = job.result()
        except (OSError, ValueError, cv2.error) as error:
            self.hide_upgrade_progress()
            if tap and (self.shop_stage or callback not in (self.device_clicked, self.dj_opened)):
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
        if self.sword_due == float("inf") and self.sword_enabled.get():
            self.reset_sword_timer()
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
        self.hide_upgrade_progress()
        self.release_mouse()
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
        if self.release_timer is not None:
            self.timer = self.root.after(100, self.scan)
            return
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
                self.pending = self.root.after(random.randint(500, 800), self.click, False)
        if box is None:
            self.status.set("請點一下目標視窗，回到前景後會自動辨識。")
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
        self.release_timer = self.root.after(805, self.finish_hold, retry, score)
        if not retry:
            self.first_point = x, y

    def release_mouse(self):
        if self.release_timer is not None:
            self.root.after_cancel(self.release_timer)
            self.release_timer = None
            u.mouse_event(4, 0, 0, 0, 0)

    def finish_hold(self, retry, score):
        self.release_mouse()
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
        if getattr(sys, "frozen", False):
            from cultivation_update import initialize_storage
            initialize_storage()
        if getattr(sys, 'frozen', False) and '--updated-launch' not in sys.argv:
            from cultivation_update import start_release
            start_release(root, App)
        else:
            App(root)
            if getattr(sys, "frozen", False) and len(sys.argv) == 3 and sys.argv[1] == "--updated-launch":
                from cultivation_update import signal_ready
                root.after_idle(signal_ready, sys.argv[2])
        root.mainloop()





