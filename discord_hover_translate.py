"""
Discord hover translator (Windows).

Hover the mouse over a message in the Discord desktop client, and a small
tooltip with a Google Translate translation appears next to the cursor.
Discord itself is not modified: the message text is read from the
accessibility tree that Chromium exposes via UI Automation.

    pip install uiautomation
    python discord_hover_translate.py            # uses the last chosen language (initially en)
    python discord_hover_translate.py --to ru

Quick translate of what you type: while the chat box contains text, a small bar with
language codes floats above it. Click a code and the text is replaced by its translation
(typed in through the editor like real keystrokes). The undo arrow restores the original.

While it runs, type commands in the console: `to en`, `quick en ko ru ja`, `delay 0.5`, `status`, `quit`.
The chosen language and delay are remembered in settings.json next to this file.
"""
import argparse
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes

# DPI awareness must be set before tkinter/uiautomation create any windows,
# otherwise cursor coordinates and tooltip placement disagree on scaled monitors.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import tkinter as tk
import uiautomation as auto
from _ctypes import COMError

user32 = ctypes.windll.user32


# --------------------------------------------------------------------------- Win32 helpers
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_under_point_is_discord(x, y):
    """Cheap pre-check before touching UI Automation."""
    hwnd = user32.WindowFromPoint(POINT(x, y))
    if not hwnd:
        return False
    root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(root, cls, 64)
    if cls.value != "Chrome_WidgetWin_1":
        return False
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(root, title, 256)
    return "Discord" in title.value


def work_area_at(x, y):
    MONITOR_DEFAULTTONEAREST = 2
    hmon = user32.MonitorFromPoint(POINT(x, y), MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def make_click_through(hwnd, alpha=245):
    """
    Apply to the top-level wrapper HWND only. WS_EX_LAYERED | WS_EX_TRANSPARENT makes
    WindowFromPoint skip the tooltip, so it never sits "under" the cursor. A layered
    window stays invisible until SetLayeredWindowAttributes has been called.
    """
    GWL_EXSTYLE = -20
    WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
    LWA_ALPHA = 0x2
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    user32.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)


def tooltip_wrapper_hwnd(tip):
    """Tk reparents a Toplevel under its own wrapper window; that wrapper is the real top-level HWND."""
    return user32.GetParent(tip.winfo_id()) or tip.winfo_id()


# Both popups stay mapped for their whole life and are parked far off-screen when not in use.
# Mapping them on demand is not an option: Tk's deiconify() activates the window, which pulls
# keyboard focus out of Discord's chat box, while showing them with SetWindowPos behind Tk's
# back leaves Tk thinking they are unmapped, and Tk then stops routing clicks to their buttons.
PARKED = (-20000, -20000)


def place_window(win, x, y):
    win.geometry(f"+{int(x)}+{int(y)}")
    win.update_idletasks()


def park_window(win):
    win.geometry("+%d+%d" % PARKED)


def start_parked(win):
    """Map the window off-screen once, so later shows are plain moves that never activate."""
    win.update_idletasks()
    park_window(win)
    win.deiconify()
    win.update_idletasks()


ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
APP_TITLE = "Discord Translator"


def apply_app_icon(root, debug=False):
    """Put icon.ico on the console window (title bar + taskbar) and on the Tk windows."""
    kernel32 = ctypes.windll.kernel32
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiscordHoverTranslate")
    except Exception:
        pass
    if not os.path.exists(ICON_FILE):
        if debug:
            print("icon not found:", ICON_FILE)
        return
    try:
        root.iconbitmap(default=ICON_FILE)
    except Exception as e:
        if debug:
            print("tk icon failed:", e)
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
        return
    kernel32.SetConsoleTitleW(APP_TITLE)
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for size, which in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        hicon = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(hwnd, WM_SETICON, which, hicon)
        elif debug:
            print(f"LoadImage {size}px failed, error {ctypes.GetLastError()}")
    if debug:
        print("console icon applied to hwnd", hex(hwnd))


# --------------------------------------------------------------------------- UIA: find message text
def _safe(fn, default=None):
    try:
        return fn()
    except COMError:
        return default


def _collect_text(control, out, depth=0):
    """Collect readable runs of a message-content group in document order."""
    if depth > 12:
        return
    for ch in _safe(control.GetChildren, []) or []:
        ct = _safe(lambda: ch.ControlType, 0)
        name = _safe(lambda: ch.Name, "") or ""
        if ct in (auto.ControlType.TextControl, auto.ControlType.HyperlinkControl,
                  auto.ControlType.ButtonControl):
            # Chromium exposes each text run as its own control; nested runs repeat
            # the parent's name, so prefer the deepest level.
            kids = _safe(ch.GetChildren, []) or []
            if kids and any(_safe(lambda: k.ControlType, 0) == auto.ControlType.TextControl for k in kids):
                _collect_text(ch, out, depth + 1)
            elif name:
                out.append(name)
        elif ct == auto.ControlType.ImageControl:
            continue  # custom emoji, stickers, attachments
        else:
            _collect_text(ch, out, depth + 1)


_EDITED_MARK = re.compile(r"\s*\((edited|изменено)\)\s*$", re.I)
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


def message_at(x, y):
    """
    Returns (message_id, text, rect) for the Discord message under the point,
    or None. rect is the bounding box of the whole message row.
    """
    el = _safe(lambda: auto.ControlFromPoint(x, y))
    if el is None:
        return None
    return message_from_element(el)


def message_from_element(el):
    """Climb from any element inside a message row to its text."""
    content = None      # nearest message-content-* group (may be a reply quote)
    message = None      # the Message group (aria-roledescription="Message")
    node = el
    for _ in range(30):
        if node is None:
            break
        aid = _safe(lambda: node.AutomationId, "") or ""
        if content is None and aid.startswith("message-content-"):
            content = node
        if _safe(lambda: node.LocalizedControlType, "") == "Message":
            message = node
            break
        if _safe(lambda: node.ControlType, 0) == auto.ControlType.WindowControl:
            break
        node = _safe(node.GetParentControl)

    if message is None:
        return None

    if content is None:
        # Cursor is over the header/avatar/margin: use the message's own content group.
        for ch in _safe(message.GetChildren, []) or []:
            aid = _safe(lambda: ch.AutomationId, "") or ""
            if aid.startswith("message-content-"):
                content = ch
                break
    if content is None:
        return None

    runs = []
    _collect_text(content, runs)
    text = " ".join(r.strip() for r in runs if r.strip())
    text = _EDITED_MARK.sub("", text)
    text = _URL.sub("", text).strip()  # links are not worth translating
    if not text:
        return None

    msg_id = _safe(lambda: content.AutomationId, "") or str(id(content))
    r = _safe(lambda: message.BoundingRectangle)
    rect = (r.left, r.top, r.right, r.bottom) if r else None
    return msg_id, text, rect


# --------------------------------------------------------------------------- settings
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULTS = {"to": "en", "delay": 0.35, "quick": ["en", "ko", "ru", "ja"]}
_LANG_CODE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = load_settings()
    data.update(changes)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("could not save settings:", e)


# --------------------------------------------------------------------------- Google Translate
_cache = {}


def translate(text, target):
    key = (text, target)
    if key in _cache:
        return _cache[key]
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
    source = data[2] if len(data) > 2 else "?"
    result = (translated.strip(), source)
    if len(_cache) > 2000:
        _cache.clear()
    _cache[key] = result
    return result


# --------------------------------------------------------------------------- composer: quick-translate what you are typing
def discord_foreground_hwnd():
    """HWND of Discord when it is the foreground window, else 0."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value != "Chrome_WidgetWin_1":
        return 0
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    return hwnd if "Discord" in title.value else 0


def foreground_is_discord():
    return discord_foreground_hwnd() != 0


COMPOSER_CLASS = "slateTextArea"
TREESCOPE_DESCENDANTS = 4
_composer = {"el": None, "hwnd": 0, "retry_after": 0.0}


def _uia_client():
    try:
        from uiautomation import uiautomation as _mod
        return _mod._AutomationClient.instance().IUIAutomation
    except Exception:
        return None


def _find_composer_element(hwnd):
    """
    Locate Discord's Slate editor inside the given window. UI Automation's native FindAll
    does this in about 50 ms; walking the tree from Python takes about a second, which is
    far too slow to poll.
    """
    win = _safe(lambda: auto.ControlFromHandle(hwnd))
    client = _uia_client()
    if win is None or client is None:
        return None
    try:
        cond = client.CreatePropertyCondition(auto.PropertyId.ControlTypeProperty,
                                              auto.ControlType.EditControl)
        found = win.Element.FindAll(TREESCOPE_DESCENDANTS, cond)
        for i in range(found.Length):
            element = found.GetElement(i)
            if COMPOSER_CLASS in (element.CurrentClassName or ""):
                return auto.Control.CreateControlFromElement(element)
    except (COMError, AttributeError, OSError):
        return None
    return None


def focused_composer():
    """
    (text, rect) of Discord's chat box while it holds keyboard focus, else None.

    The element is cached: re-finding it costs 50 ms, reading focus/text/rect off the cached
    element costs well under a millisecond. Note that auto.GetFocusedControl is useless here,
    since it reports a stale focused element from whichever app last set one; the editor's own
    HasKeyboardFocus flag is accurate.
    """
    hwnd = discord_foreground_hwnd()
    if not hwnd:
        return None
    if _composer["hwnd"] != hwnd:
        _composer.update(el=None, hwnd=hwnd, retry_after=0.0)

    el = _composer["el"]
    if el is not None:
        try:
            if not el.HasKeyboardFocus:
                return None
            value = el.GetValuePattern().Value or ""
            r = el.BoundingRectangle
            # Slate keeps a zero-width BOM marker in an empty editor
            return value.replace("\ufeff", "").strip(), (r.left, r.top, r.right, r.bottom)
        except (COMError, AttributeError):
            _composer["el"] = None  # channel switched and the editor was rebuilt

    if time.monotonic() < _composer["retry_after"]:
        return None
    el = _find_composer_element(hwnd)
    _composer["el"] = el
    if el is None:
        _composer["retry_after"] = time.monotonic() + 1.5
        return None
    return focused_composer()


# --- synthetic keyboard input: the replacement goes through Discord's editor like real typing
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_BACK, VK_CONTROL, VK_A, VK_V = 0x08, 0x11, 0x41, 0x56


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key(vk=0, scan=0, flags=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def _press(vk):
    return [_key(vk), _key(vk, flags=KEYEVENTF_KEYUP)]


def _send(keys):
    arr = (INPUT * len(keys))(*keys)
    return user32.SendInput(len(keys), arr, ctypes.sizeof(INPUT)) == len(keys)


def _chord(vk):
    """Ctrl + key."""
    return [_key(VK_CONTROL)] + _press(vk) + [_key(VK_CONTROL, flags=KEYEVENTF_KEYUP)]


# --- clipboard, through Win32 rather than Tk. Tk hands clipboard data over lazily, only
# while its event loop is pumping, so a paste fired right after setting it arrives empty.
kernel32 = ctypes.windll.kernel32
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.restype = ctypes.c_void_p


_clipboard = {"theirs": None, "ours": None}


def _open_clipboard(attempts=10):
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_get_text():
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text):
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, buffer, size)
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def replace_composer_text(root, new_text):
    """
    Replace everything in the focused chat box with new_text: select all, delete, paste.

    Pasting rather than typing is deliberate. Electron ignores synthetic Unicode key events,
    because KEYEVENTF_UNICODE arrives as VK_PACKET and Chromium drops it, so typed characters
    never reach Discord's editor. The previous clipboard text is put back afterwards.
    """
    _send(_chord(VK_A))
    time.sleep(0.05)
    _send(_press(VK_BACK))
    if not new_text:
        return
    time.sleep(0.05)
    current = clipboard_get_text()
    if current != _clipboard["ours"]:
        # Only remember a clipboard the user put there, not one of our own pastes, so two
        # quick translations in a row still restore the original text.
        _clipboard["theirs"] = current
    if not clipboard_set_text(new_text):
        print("could not put the translation on the clipboard")
        return
    _clipboard["ours"] = new_text
    time.sleep(0.05)
    _send(_chord(VK_V))

    def restore():
        if _clipboard["theirs"] is not None and clipboard_get_text() == _clipboard["ours"]:
            clipboard_set_text(_clipboard["theirs"])
            _clipboard["ours"] = None

    root.after(700, restore)


def make_no_activate(hwnd):
    """Clicks on this window must not steal keyboard focus from Discord's editor."""
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)


class QuickTranslateBar:
    """A small strip above Discord's chat box: click a language to translate what you typed."""
    BG, BORDER, FG, DIM, HOVER = "#1e1f22", "#3f4147", "#dbdee1", "#80848e", "#404249"

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.BORDER)
        self.frame = tk.Frame(self.win, bg=self.BG)
        self.frame.pack(padx=1, pady=1)
        self.buttons = {}
        self.undo_btn = None
        self.original = None      # text before the last replacement, for undo
        self.busy = False
        self.visible = False
        self.last_rect = None
        self.rebuild()
        make_no_activate(tooltip_wrapper_hwnd(self.win))
        start_parked(self.win)
        make_no_activate(tooltip_wrapper_hwnd(self.win))

    def _label(self, text, cmd, bold=True):
        weight = "bold" if bold else "normal"
        lbl = tk.Label(self.frame, text=text, bg=self.BG, fg=self.FG, padx=7, pady=2, cursor="hand2",
                       font=("Segoe UI", max(self.app.args.font_size - 1, 8), weight))
        lbl.bind("<Enter>", lambda e: lbl.configure(bg=self.HOVER))
        lbl.bind("<Leave>", lambda e: lbl.configure(bg=self.BG))
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.pack(side="left")
        return lbl

    def rebuild(self):
        for w in self.frame.winfo_children():
            w.destroy()
        tk.Label(self.frame, text="⇄", bg=self.BG, fg=self.DIM, padx=6,
                 font=("Segoe UI", max(self.app.args.font_size - 1, 8))).pack(side="left")
        self.buttons = {code: self._label(code.upper(), lambda c=code: self.on_lang(c))
                        for code in self.app.args.quick}
        self.undo_btn = self._label("↶", self.on_undo, bold=False)
        self.undo_btn.pack_forget()
        self.win.update_idletasks()
        self.last_rect = None

    # ---- called from the Tk loop a few times per second
    def refresh(self):
        if self.busy:
            return
        comp = focused_composer() if foreground_is_discord() else None
        if comp is None:
            self.hide()
            return
        text, rect = comp
        if not text:
            self.original = None
            self.undo_btn.pack_forget()
            self.hide()
            return
        if not self.visible or rect != self.last_rect:
            self.place(rect)

    def place(self, rect):
        self.last_rect = rect
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = work_area_at(rect[0], rect[1])
        x = min(max(rect[0], left), right - w)
        y = rect[1] - h - 6
        if y < top:
            y = rect[3] + 6
        place_window(self.win, x, y)
        self.visible = True

    def hide(self):
        if self.visible:
            park_window(self.win)
            self.visible = False
            self.last_rect = None

    # ---- actions
    def on_lang(self, code):
        if self.busy:
            return
        comp = focused_composer() if foreground_is_discord() else None
        if comp is None or not comp[0]:
            return
        text = comp[0]
        self.busy = True
        if code in self.buttons:
            self.buttons[code].configure(text="…")

        def worker():
            try:
                res, err = translate(text, code), None
            except Exception as e:
                res, err = None, e
            self.app.results.put((self._apply, (code, text, res, err)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, code, text, res, err):
        self.busy = False
        if code in self.buttons:
            self.buttons[code].configure(text=code.upper())
        if err is not None:
            print("quick translate error:", repr(err))
            return
        translated = res[0]
        if not translated or translated == text:
            return
        # Type only while the chat box still has focus, so keystrokes never land elsewhere.
        if not foreground_is_discord() or focused_composer() is None:
            print("chat box lost focus, translation not applied")
            return
        replace_composer_text(self.app.root, translated)
        self.original = text
        self.undo_btn.pack(side="left")
        self.last_rect = None  # size changed, re-place on next refresh

    def on_undo(self):
        if self.busy or not self.original:
            return
        if not foreground_is_discord() or focused_composer() is None:
            return
        replace_composer_text(self.app.root, self.original)
        self.original = None
        self.undo_btn.pack_forget()
        self.last_rect = None


# --------------------------------------------------------------------------- App
class HoverTranslator:
    POLL_MS = 50

    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()
        self.root.withdraw()
        apply_app_icon(self.root, debug=args.debug)

        self.tip = tk.Toplevel(self.root)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.attributes("-alpha", 0.96)
        self.tip.configure(bg="#1e1f22")
        frame = tk.Frame(self.tip, bg="#1e1f22", highlightbackground="#3f4147", highlightthickness=1)
        frame.pack(fill="both", expand=True)
        self.header = tk.Label(frame, text="", bg="#1e1f22", fg="#80848e",
                               font=("Segoe UI", max(args.font_size - 3, 7)), anchor="w")
        self.header.pack(fill="x", padx=10, pady=(6, 0))
        self.body = tk.Label(frame, text="", bg="#1e1f22", fg="#dbdee1", justify="left",
                             wraplength=args.width, font=("Segoe UI", args.font_size), anchor="w")
        self.body.pack(padx=10, pady=(2, 8))
        self.tip.update_idletasks()
        make_click_through(tooltip_wrapper_hwnd(self.tip))
        start_parked(self.tip)
        make_click_through(tooltip_wrapper_hwnd(self.tip))

        self.last_pos = cursor_pos()
        self.still_since = time.monotonic()
        self.handled_pos = None
        self.current_id = None
        self.current_rect = None
        self.last_check = 0.0
        self.request_seq = 0
        self.last_keepalive = time.monotonic()
        self.last_composer_check = 0.0
        self.results = queue.Queue()  # (callable, args) handed over by worker threads
        self.bar = QuickTranslateBar(self)

    # ---- tooltip
    def show(self, x, y, text, header):
        self.header.configure(text=header)
        self.body.configure(text=text)
        self.tip.update_idletasks()
        w, h = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        left, top, right, bottom = work_area_at(x, y)
        tx, ty = x + 18, y + 22
        if tx + w > right:
            tx = max(left, x - w - 8)
        if ty + h > bottom:
            ty = max(top, y - h - 8)
        place_window(self.tip, tx, ty)

    def hide(self):
        park_window(self.tip)
        self.current_id = None
        self.current_rect = None

    # ---- main loop
    def poll(self):
        try:
            self._drain_results()
            self.tick()
        except Exception as e:  # never let one bad frame kill the loop
            if self.args.debug:
                print("tick error:", repr(e))
        self.root.after(self.POLL_MS, self.poll)

    def _drain_results(self):
        """Run callbacks queued by worker threads; Tk must only be touched from this thread."""
        while True:
            try:
                fn, args = self.results.get_nowait()
            except queue.Empty:
                return
            fn(*args)

    def tick(self):
        now = time.monotonic()
        if now - self.last_composer_check >= 0.3:
            self.last_composer_check = now
            self.bar.refresh()
        x, y = cursor_pos()
        if (x, y) != self.last_pos:
            self.last_pos = (x, y)
            self.still_since = now
            self.handled_pos = None
            if self.current_rect and not self._inside(x, y, self.current_rect):
                self.hide()
            return

        if not window_under_point_is_discord(x, y):
            if self.current_id:
                self.hide()
            self._keepalive(now)
            return

        stable = (now - self.still_since) >= self.args.delay
        if not stable:
            return
        # Re-check the element under the cursor every so often even when still,
        # so scrolling with the wheel picks up the new message.
        if self.handled_pos == (x, y) and (now - self.last_check) < 0.7:
            return
        self.handled_pos = (x, y)
        self.last_check = now

        found = message_at(x, y)
        if found is None:
            if self.current_id:
                self.hide()
            return
        msg_id, text, rect = found
        if msg_id == self.current_id:
            self.current_rect = rect
            return
        self.current_id, self.current_rect = msg_id, rect
        if self.args.debug:
            print(f"[{msg_id}] {text[:80]!r}")
        self._request_translation(text, x, y, msg_id)

    def _inside(self, x, y, rect):
        l, t, r, b = rect
        return l <= x <= r and t <= y <= b

    def _keepalive(self, now):
        """Chromium switches accessibility off when nobody asks for a while; poke it."""
        if now - self.last_keepalive < 20:
            return
        self.last_keepalive = now
        for c in _safe(auto.GetRootControl().GetChildren, []) or []:
            if _safe(lambda: c.ClassName, "") == "Chrome_WidgetWin_1" and "Discord" in (_safe(lambda: c.Name, "") or ""):
                for ch in _safe(c.GetChildren, []) or []:
                    _safe(lambda: ch.Name)
                break

    def _request_translation(self, text, x, y, msg_id):
        self.request_seq += 1
        seq = self.request_seq
        cached = _cache.get((text, self.args.to))
        if cached:
            self._deliver(seq, msg_id, x, y, cached, None)
            return
        self.show(x, y, "Translating...", f"→ {self.args.to}")

        def worker():
            try:
                res = translate(text, self.args.to)
                err = None
            except Exception as e:
                res, err = None, e
            self.results.put((self._deliver, (seq, msg_id, x, y, res, err)))

        threading.Thread(target=worker, daemon=True).start()

    def _deliver(self, seq, msg_id, x, y, res, err):
        if seq != self.request_seq or msg_id != self.current_id:
            return  # user already moved on
        if err is not None:
            msg = "Google Translate did not respond"
            if "429" in str(err):
                msg = "Google is rate-limiting requests, wait a bit"
            self.show(x, y, msg, "error")
            if self.args.debug:
                print("translate error:", repr(err))
            return
        translated, source = res
        if source == self.args.to and not self.args.show_same:
            park_window(self.tip)  # keep current_id so we don't re-resolve this message every 0.7 s
            return
        self.show(x, y, translated, f"{source} → {self.args.to}")

    # ---- console commands
    HELP = ("Commands:\n"
            "  to <lang>      target language for hover tooltips, e.g. 'to en', 'to zh-CN'\n"
            "  quick <langs>  languages in the bar above the chat box, e.g. 'quick en ko ru ja'\n"
            "  delay <sec>    hover delay before translating, e.g. 'delay 0.5'\n"
            "  same on|off    also show messages that are already in the target language\n"
            "  status         show current settings\n"
            "  quit           exit")

    def _console_loop(self):
        """Runs in a background thread; commands are applied on the Tk thread."""
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    self.results.put((self._handle_command, (line,)))
        except (OSError, ValueError):
            pass  # no usable console (e.g. started with pythonw)

    def _handle_command(self, line):
        parts = line.split()
        cmd, rest = parts[0].lower(), parts[1:]
        if cmd in ("to", "lang", "target") and rest:
            code = rest[0]
            if not _LANG_CODE.match(code):
                print(f"'{code}' does not look like a language code (examples: en, ru, ja, zh-CN)")
                return
            self.args.to = code
            self.hide()  # the next hover re-translates into the new language
            save_settings(to=code)
            print(f"target language: {code}")
        elif cmd == "quick" and rest:
            codes = [c for c in rest if _LANG_CODE.match(c)]
            if not codes:
                print("usage: quick en ko ru ja")
                return
            self.args.quick = codes
            save_settings(quick=codes)
            self.bar.rebuild()
            print("quick languages: " + " ".join(codes))
        elif cmd == "delay" and rest:
            try:
                self.args.delay = max(0.05, float(rest[0]))
            except ValueError:
                print("usage: delay <seconds>")
                return
            save_settings(delay=self.args.delay)
            print(f"hover delay: {self.args.delay:.2f}s")
        elif cmd == "same" and rest and rest[0].lower() in ("on", "off"):
            self.args.show_same = rest[0].lower() == "on"
            print(f"show same-language messages: {'on' if self.args.show_same else 'off'}")
        elif cmd == "status":
            print(f"target: {self.args.to} | quick: {' '.join(self.args.quick)} | delay: {self.args.delay:.2f}s | "
                  f"same-language: {'on' if self.args.show_same else 'off'} | cached translations: {len(_cache)}")
        elif cmd in ("quit", "exit", "q"):
            print("bye")
            self.root.quit()
        elif cmd in ("help", "?"):
            print(self.HELP)
        else:
            print(f"unknown command '{cmd}', type 'help'")

    def run(self):
        print(f"Discord hover translator | target: {self.args.to} | hover delay: {self.args.delay:.2f}s")
        print(self.HELP)
        threading.Thread(target=self._console_loop, daemon=True).start()
        self.root.after(self.POLL_MS, self.poll)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Hover translator for the Discord desktop client")
    p.add_argument("--to", default=None, help="target language code (default: last used, initially en)")
    p.add_argument("--delay", type=float, default=None, help="hover delay in seconds before translating")
    p.add_argument("--width", type=int, default=460, help="tooltip wrap width in px")
    p.add_argument("--font-size", type=int, default=11)
    p.add_argument("--show-same", action="store_true", help="show tooltip even when the message is already in the target language")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    saved = load_settings()
    if args.to is None:
        args.to = saved.get("to", DEFAULTS["to"])
    if args.delay is None:
        args.delay = saved.get("delay", DEFAULTS["delay"])
    quick = saved.get("quick", DEFAULTS["quick"])
    args.quick = [c for c in quick if isinstance(c, str) and _LANG_CODE.match(c)] or DEFAULTS["quick"]
    return args


if __name__ == "__main__":
    HoverTranslator(parse_args()).run()
