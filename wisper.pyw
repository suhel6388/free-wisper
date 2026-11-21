"""Wisper - hold a key, speak, and the text gets typed wherever your cursor is.

Push-to-talk dictation using faster-whisper running locally on this PC.
UI built with CustomTkinter with light / dark / system appearance modes.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.messagebox as messagebox
import urllib.request

import customtkinter as ctk
import numpy as np
import sounddevice as sd
from pynput import keyboard

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
MODEL_DIR = os.path.join(HERE, "models")

SAMPLE_RATE = 16000
MIN_SECONDS = 0.35          # shorter recordings are treated as an accidental tap
MIN_LEVEL = 0.004           # recordings quieter than this are ignored

MODEL_CHOICES = ("tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "large-v3")
MODEL_REPOS = {
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base.en": "Systran/faster-whisper-base.en",
    "small.en": "Systran/faster-whisper-small.en",
    "medium.en": "Systran/faster-whisper-medium.en",
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "large-v3": "Systran/faster-whisper-large-v3",
}
MODEL_REQUIRED = ("config.json", "model.bin", "tokenizer.json")
MODEL_OPTIONAL = ("preprocessor_config.json", "vocabulary.txt", "vocabulary.json")

LANGUAGE_CHOICES = (("English", "en"), ("Auto detect", ""))

DEFAULTS = {
    "model": "base.en",
    "language": "en",
    "hotkey": "ctrl_l",
    "input_device": -1,     # -1 = system default
    "press_enter": False,
    "appearance": "system",  # light | dark | system
}

APPEARANCE_ORDER = ("light", "dark", "system")
APPEARANCE_META = {
    "light": ("\u2600", "Light"),
    "dark": ("\u263e", "Dark"),
    "system": ("\u25d0", "System"),
}

# --------------------------------------------------------------------------- palette
# Every colour is a (light, dark) pair, so CustomTkinter swaps it automatically.

BG = ("#F5F7FB", "#0B0F19")
CARD = ("#FFFFFF", "#151B2B")
FIELD = ("#F1F4FA", "#1B2334")
FIELD_HOVER = ("#E7ECF6", "#232C42")
BORDER = ("#E3E8F0", "#232C40")
SHADOW = ("#E9EDF5", "#05080E")
ACCENT = ("#4F46E5", "#6366F1")
ACCENT_HOVER = ("#4338CA", "#818CF8")
TEXT = ("#111827", "#F8FAFC")
MUTED = ("#6B7280", "#94A3B8")

STATE_RECORDING = (("#FDECEC", "#3A1A1F"), ("#B42318", "#FDA4AF"))
STATE_BUSY = (("#FFF6E5", "#372C15"), ("#946200", "#FBBF24"))
STATE_ERROR = STATE_RECORDING
STATE_CAPTURE = (("#E8EEFC", "#202844"), ("#1D4ED8", "#93B4FF"))

PRETTY_KEYS = {
    "ctrl_l": "Left Ctrl",
    "ctrl_r": "Right Ctrl",
    "alt_l": "Left Alt",
    "alt_r": "Right Alt",
    "shift_l": "Left Shift",
    "shift_r": "Right Shift",
    "caps_lock": "Caps Lock",
    "space": "Space",
    "tab": "Tab",
    "esc": "Esc",
    "enter": "Enter",
}


# --------------------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULTS)
    fresh = not os.path.exists(CONFIG_PATH)
    try:
        # utf-8-sig so a BOM from external editors does not reset the settings
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            stored = json.load(f)
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except Exception:
        pass
    if cfg["appearance"] not in APPEARANCE_ORDER:
        cfg["appearance"] = DEFAULTS["appearance"]
    if fresh:
        # First run: prefer a model that is already downloaded.
        for name in MODEL_CHOICES:
            if model_is_ready(model_path(name)):
                cfg["model"] = name
                break
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def pretty_key(name):
    return PRETTY_KEYS.get(name) or name.replace("_", " ").title()


def key_name(key):
    if isinstance(key, keyboard.Key):
        return key.name
    char = getattr(key, "char", None)
    if char:
        return char
    vk = getattr(key, "vk", None)
    return f"vk{vk}" if vk is not None else str(key)


# --------------------------------------------------------------------------- model files

def model_path(name):
    return os.path.join(MODEL_DIR, name)


def model_is_ready(path):
    return all(
        os.path.isfile(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) > 0
        for f in MODEL_REQUIRED
    )


def _fetch(url, dest, label, on_progress):
    """Download with resume support. Leftover bytes go to <dest>.part."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "Wisper"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        resuming = have > 0 and response.getcode() == 206
        if not resuming:
            have = 0
        total = int(response.headers.get("Content-Length") or 0) + have
        done = have
        last_report = 0.0
        with open(part, "ab" if resuming else "wb") as out:
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report > 0.5:
                    last_report = now
                    on_progress(label, done, total)
    os.replace(part, dest)


def download_model(name, on_progress, attempts=40):
    """Fetch the model files for `name` into models/<name>/ (resumes on failure)."""
    repo = MODEL_REPOS[name]
    target = model_path(name)
    os.makedirs(target, exist_ok=True)
    for filename in MODEL_OPTIONAL + MODEL_REQUIRED:
        dest = os.path.join(target, filename)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue
        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
        required = filename in MODEL_REQUIRED
        for attempt in range(attempts):
            try:
                _fetch(url, dest, filename, on_progress)
                break
            except Exception:
                if not required:
                    break
                if attempt == attempts - 1:
                    raise
                time.sleep(2)


# --------------------------------------------------------------------------- clipboard / typing

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.OpenClipboard.argtypes = [wt.HWND]
_user32.OpenClipboard.restype = wt.BOOL
_user32.GetClipboardData.argtypes = [wt.UINT]
_user32.GetClipboardData.restype = wt.HANDLE
_user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
_user32.SetClipboardData.restype = wt.HANDLE
_kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wt.HGLOBAL
_kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]

_kbd = keyboard.Controller()


def get_clipboard_text():
    for _ in range(10):
        if _user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text):
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = _kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        return True
    finally:
        _user32.CloseClipboard()


def paste_text(text, press_enter=False, restore_delay=0.5):
    """Put text into whatever window currently has focus."""
    previous = get_clipboard_text()
    pasted = False
    if set_clipboard_text(text):
        time.sleep(0.02)
        try:
            with _kbd.pressed(keyboard.Key.ctrl):
                _kbd.press("v")
                _kbd.release("v")
            pasted = True
        except Exception:
            pasted = False
    if not pasted:
        _kbd.type(text)
    else:
        # Give the target app time to read the clipboard before putting it back.
        time.sleep(restore_delay)
        if previous and previous != text:
            set_clipboard_text(previous)
    if press_enter:
        time.sleep(0.02)
        _kbd.press(keyboard.Key.enter)
        _kbd.release(keyboard.Key.enter)


# --------------------------------------------------------------------------- audio

def list_microphones():
    choices = [("System default", -1)]
    try:
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                choices.append((f"{index}: {device['name']}", index))
    except Exception:
        pass
    return choices


def resample(audio, rate, target=SAMPLE_RATE):
    if rate == target or len(audio) == 0:
        return audio.astype(np.float32)
    count = max(1, int(round(len(audio) * target / rate)))
    return np.interp(
        np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio
    ).astype(np.float32)


class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None
        self.samplerate = SAMPLE_RATE
        self.level = 0.0

    @property
    def is_open(self):
        return self.stream is not None

    def start(self, device):
        self.frames = []
        self.level = 0.0
        device = None if device is None or device < 0 else device
        try:
            self._open(device, SAMPLE_RATE)
        except Exception:
            info = sd.query_devices(device, "input")
            self._open(device, int(info["default_samplerate"]))
        try:
            self.stream.start()
        except Exception:
            self.abort()
            raise

    def _open(self, device, rate):
        self.samplerate = rate
        self.stream = sd.InputStream(
            samplerate=rate,
            channels=1,
            dtype="float32",
            device=device,
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        self.frames.append(indata.copy())
        self.level = float(np.sqrt(np.mean(indata ** 2)))

    def stop(self):
        """Return (audio, seconds, peak_level) and close the stream."""
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        frames, self.frames = self.frames, []
        if not frames:
            return np.zeros(0, dtype=np.float32), 0.0, 0.0
        audio = np.concatenate(frames, axis=0)[:, 0]
        peak = float(np.max(np.abs(audio)))
        audio = resample(audio, self.samplerate)
        return audio, len(audio) / SAMPLE_RATE, peak

    def abort(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.frames = []


# --------------------------------------------------------------------------- transcription worker

def _vad_available():
    """Voice-activity filtering drops silence and noise-only clips."""
    global _VAD
    if _VAD is None:
        try:
            import onnxruntime  # noqa: F401  (silero VAD backend)
            _VAD = True
        except Exception:
            _VAD = False
    return _VAD


_VAD = None


class Worker(threading.Thread):
    """Loads the model and transcribes recordings off the UI thread."""

    def __init__(self, events):
        super().__init__(daemon=True)
        self.events = events
        self.jobs = queue.Queue()
        self.model = None
        self.model_name = None
        self.pending_name = "model"

    def request_model(self, name):
        if name != self.model_name:
            self.jobs.put(("load", name))

    def request_transcribe(self, name, audio, language):
        self.jobs.put(("transcribe", (name, audio, language)))

    def run(self):
        while True:
            kind, payload = self.jobs.get()
            try:
                if kind == "load":
                    self._ensure_model(payload)
                else:
                    name, audio, language = payload
                    model = self._ensure_model(name)
                    self.events.put(("status", ("busy", "Transcribing...")))
                    segments, _info = model.transcribe(
                        audio,
                        language=language or None,
                        beam_size=1,
                        vad_filter=_vad_available(),
                        vad_parameters={"min_silence_duration_ms": 300},
                        condition_on_previous_text=False,
                    )
                    text = "".join(segment.text for segment in segments).strip()
                    self.events.put(("text", text))
            except Exception as exc:
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    def _ensure_model(self, name):
        if self.model is not None and self.model_name == name:
            return self.model
        self.pending_name = name
        path = model_path(name)
        if not model_is_ready(path):
            self.events.put(("status", ("busy", f"Downloading {name}...")))
            download_model(name, self._download_progress)
        self.events.put(("status", ("busy", f"Loading {name}...")))
        from faster_whisper import WhisperModel

        model = WhisperModel(
            path,
            device="cpu",
            compute_type="int8",
            cpu_threads=min(8, os.cpu_count() or 4),
        )
        self.model = model
        self.model_name = name
        self.events.put(("loaded", name))
        return model

    def _download_progress(self, filename, done, total):
        mb = 1024 * 1024
        name = self.pending_name
        if filename != "model.bin":
            # Tokenizer/config files are tiny; numbers would just flicker.
            text = f"Preparing {name}..."
        elif total:
            percent = min(99, int(done * 100 / total))
            text = f"Downloading {name}: {percent}% ({done / mb:.0f}/{total / mb:.0f} MB)"
        else:
            text = f"Downloading {name}: {done / mb:.0f} MB"
        self.events.put(("status", ("busy", text)))


# --------------------------------------------------------------------------- app

class App:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        ctk.set_appearance_mode(self.config["appearance"])
        self.events = queue.Queue()
        self.recorder = Recorder()
        self.worker = Worker(self.events)
        self.capturing = False
        self.recording = False
        self.hotkey_down = False
        self.busy = 0
        self.busy_text = "Working..."
        self.shortcut_used = False
        self.mic_choices = list_microphones()

        self._build_ui()
        self.worker.start()
        self._start_listener()
        self.worker.request_model(self.config["model"])
        self.busy += 1  # counts as loading until "loaded" arrives
        self._refresh_status()
        self.root.after(40, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- theme -------------------------------------------------------------
    def _cycle_theme(self):
        index = APPEARANCE_ORDER.index(self.config["appearance"])
        self.set_appearance(APPEARANCE_ORDER[(index + 1) % len(APPEARANCE_ORDER)])

    def set_appearance(self, mode, animate=True):
        """light | dark | system, persisted and applied to every widget."""
        if mode not in APPEARANCE_ORDER or mode == self.config["appearance"]:
            self._sync_theme_button()
            return
        self.config["appearance"] = mode
        save_config(self.config)
        if animate:
            self._animate_theme_switch(mode)
        else:
            ctk.set_appearance_mode(mode)
            self._sync_theme_button()

    def _animate_theme_switch(self, mode):
        """Short alpha dip so the palette swap does not flash."""
        down = (0.97, 0.93, 0.90)
        up = (0.92, 0.96, 1.0)

        def fade_up(i=0):
            if i >= len(up):
                return
            self.root.attributes("-alpha", up[i])
            self.root.after(16, lambda: fade_up(i + 1))

        def fade_down(i=0):
            if i < len(down):
                self.root.attributes("-alpha", down[i])
                self.root.after(16, lambda: fade_down(i + 1))
                return
            ctk.set_appearance_mode(mode)
            self._sync_theme_button()
            self._refresh_status()
            fade_up()

        fade_down()

    def _sync_theme_button(self):
        icon, label = APPEARANCE_META[self.config["appearance"]]
        self.theme_btn.configure(text=f"{icon}  {label}")
        self.root.after(30, self._apply_titlebar)

    def _apply_titlebar(self):
        """Keep the native title bar in step with the in-app palette."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dark = ctypes.c_int(1 if ctk.get_appearance_mode() == "Dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        except Exception:
            pass

    # -- ui ----------------------------------------------------------------
    def _build_ui(self):
        root = self.root
        root.title("Wisper")
        root.geometry("500x700")
        root.minsize(460, 640)
        root.configure(fg_color=BG)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(18, 10))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(titles, text="Wisper", font=("Segoe UI", 24, "bold"),
                     text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(titles, text="Local voice typing - press, speak, release.",
                     font=("Segoe UI", 12), text_color=MUTED,
                     anchor="w").pack(anchor="w", pady=(2, 0))

        self.theme_btn = ctk.CTkButton(
            header, text="", width=108, height=34, corner_radius=17,
            fg_color=CARD, hover_color=FIELD_HOVER, border_width=1,
            border_color=BORDER, text_color=TEXT,
            font=("Segoe UI Symbol", 13, "bold"), command=self._cycle_theme,
        )
        self.theme_btn.pack(side="right")

        # status card, with a soft shadow ring behind it
        shadow = ctk.CTkFrame(root, fg_color=SHADOW, corner_radius=20)
        shadow.pack(fill="x", padx=20, pady=(4, 12))
        card = ctk.CTkFrame(shadow, fg_color=CARD, corner_radius=18,
                            border_width=1, border_color=BORDER)
        card.pack(fill="both", expand=True, padx=2, pady=2)

        self.status_pill = ctk.CTkFrame(card, fg_color=FIELD, corner_radius=14)
        self.status_pill.pack(fill="x", padx=12, pady=(12, 8))
        self.status = ctk.CTkLabel(self.status_pill, text="",
                                   font=("Segoe UI", 16, "bold"),
                                   text_color=TEXT, height=46)
        self.status.pack(fill="x", padx=10)

        self.level = ctk.CTkProgressBar(card, height=6, corner_radius=3,
                                        progress_color=ACCENT, fg_color=FIELD)
        self.level.set(0)
        self.level.pack(fill="x", padx=14, pady=(0, 10))

        self.hint = ctk.CTkLabel(card, text="", font=("Segoe UI", 11),
                                 text_color=MUTED, anchor="w", justify="left",
                                 wraplength=430)
        self.hint.pack(fill="x", padx=14, pady=(0, 12))

        # settings card
        settings = ctk.CTkFrame(root, fg_color=CARD, corner_radius=18,
                                border_width=1, border_color=BORDER)
        settings.pack(fill="x", padx=20, pady=(0, 12))
        settings.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(settings, text="Settings", font=("Segoe UI", 13, "bold"),
                     text_color=TEXT, anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 4))

        def row(r, text):
            ctk.CTkLabel(settings, text=text, font=("Segoe UI", 12),
                         text_color=MUTED, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(16, 12), pady=6)

        menu_style = dict(height=32, corner_radius=10, fg_color=FIELD,
                          button_color=FIELD, button_hover_color=FIELD_HOVER,
                          text_color=TEXT, dropdown_fg_color=CARD,
                          dropdown_hover_color=FIELD_HOVER,
                          dropdown_text_color=TEXT, font=("Segoe UI", 12),
                          anchor="w")

        row(1, "Model")
        self.model_menu = ctk.CTkOptionMenu(settings, values=list(MODEL_CHOICES),
                                            command=self._on_model, **menu_style)
        self.model_menu.set(self.config["model"])
        self.model_menu.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=6)

        row(2, "Language")
        self.language_menu = ctk.CTkOptionMenu(
            settings, values=[label for label, _ in LANGUAGE_CHOICES],
            command=self._on_language, **menu_style)
        self.language_menu.set(next(
            (label for label, code in LANGUAGE_CHOICES if code == self.config["language"]),
            LANGUAGE_CHOICES[0][0]))
        self.language_menu.grid(row=2, column=1, sticky="ew", padx=(0, 16), pady=6)

        row(3, "Microphone")
        labels = [label for label, _ in self.mic_choices]
        self.mic_menu = ctk.CTkOptionMenu(settings, values=labels,
                                          command=self._on_mic, **menu_style)
        self.mic_menu.set(next(
            (label for label, index in self.mic_choices
             if index == self.config["input_device"]), labels[0]))
        self.mic_menu.grid(row=3, column=1, sticky="ew", padx=(0, 16), pady=6)

        row(4, "Push to talk")
        self.hotkey_btn = ctk.CTkButton(
            settings, text="", height=32, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color=("#FFFFFF", "#FFFFFF"), font=("Segoe UI", 12, "bold"),
            command=self._begin_hotkey_capture)
        self.hotkey_btn.grid(row=4, column=1, sticky="ew", padx=(0, 16), pady=6)

        self.enter_var = tk.BooleanVar(value=bool(self.config["press_enter"]))
        self.enter_switch = ctk.CTkSwitch(
            settings, text="Press Enter after typing", variable=self.enter_var,
            onvalue=True, offvalue=False, command=self._on_enter_toggle,
            font=("Segoe UI", 12), text_color=TEXT, progress_color=ACCENT,
            fg_color=FIELD, button_color=CARD, button_hover_color=FIELD_HOVER,
            width=210)
        self.enter_switch.grid(row=5, column=0, columnspan=2, sticky="w",
                               padx=16, pady=(6, 14))

        # transcriptions card
        history = ctk.CTkFrame(root, fg_color=CARD, corner_radius=18,
                               border_width=1, border_color=BORDER)
        history.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        head = ctk.CTkFrame(history, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(head, text="Transcriptions", font=("Segoe UI", 13, "bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkButton(head, text="Clear", width=56, height=26, corner_radius=13,
                      fg_color="transparent", hover_color=FIELD_HOVER,
                      text_color=MUTED, font=("Segoe UI", 11),
                      command=self._clear_log).pack(side="right")

        self.log = ctk.CTkTextbox(
            history, corner_radius=12, fg_color=FIELD, border_width=1,
            border_color=BORDER, text_color=TEXT, font=("Segoe UI", 12),
            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
        self.log.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        self._sync_theme_button()
        self._sync_hotkey_button()
        self.hint.configure(text=self._model_hint())

    def _model_hint(self):
        return ("Runs on your CPU. base.en is a good balance; small.en is more accurate "
                "but slower. A new model downloads once (~75 MB tiny, ~145 MB base, "
                "~480 MB small).")

    def _sync_hotkey_button(self):
        label = "Press any key..." if self.capturing else pretty_key(self.config["hotkey"])
        self.hotkey_btn.configure(text=f"Hold to talk - {label}")

    # -- status ------------------------------------------------------------
    def _refresh_status(self):
        if self.capturing:
            text = "Press a key to use for push-to-talk"
            pill, text_color = STATE_CAPTURE
        elif self.recording:
            text = "Recording - release to type"
            pill, text_color = STATE_RECORDING
        elif self.busy > 0:
            text = self.busy_text
            pill, text_color = STATE_BUSY
        else:
            text = f"Hold {pretty_key(self.config['hotkey'])} and speak"
            pill, text_color = FIELD, TEXT
        self.status.configure(text=text, text_color=text_color)
        self.status_pill.configure(fg_color=pill)
        # At rest the bar would show a stray accent cap, so blend it into the track.
        self.level.configure(progress_color=ACCENT if self.recording else FIELD)
        if not self.recording:
            self.level.set(0)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # -- hotkey ------------------------------------------------------------
    def _start_listener(self):
        self.listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self.listener.daemon = True
        self.listener.start()

    def _on_press(self, key):
        name = key_name(key)
        if self.capturing:
            self.capturing = False
            self.events.put(("hotkey", name))
            return
        if name == self.config["hotkey"]:
            if not self.hotkey_down:
                self.hotkey_down = True
                self.events.put(("press", None))
        elif self.hotkey_down:
            # Ctrl+C and friends: this is a shortcut, not dictation.
            self.shortcut_used = True

    def _on_release(self, key):
        if self.hotkey_down and key_name(key) == self.config["hotkey"]:
            self.hotkey_down = False
            self.events.put(("release", None))

    def _begin_hotkey_capture(self):
        self.capturing = True
        self._sync_hotkey_button()
        self._refresh_status()

    # -- recording ---------------------------------------------------------
    def _start_recording(self):
        try:
            self.recorder.start(self.config["input_device"])
        except Exception as exc:
            self._log(f"[mic error] {exc}")
            self.events.put(("error", str(exc)))
            return
        self.recording = True
        self.shortcut_used = False
        self._refresh_status()

    def _stop_recording(self):
        audio, seconds, peak = self.recorder.stop()
        self.recording = False
        self.level.set(0)
        if self.shortcut_used:
            self.shortcut_used = False
            self._refresh_status()
            return
        if seconds < MIN_SECONDS or peak < MIN_LEVEL:
            self._refresh_status()
            return
        self.busy += 1
        language = dict(LANGUAGE_CHOICES).get(self.language_menu.get(), "")
        self.worker.request_transcribe(self.config["model"], audio, language)
        self._refresh_status()

    # -- settings ----------------------------------------------------------
    def _on_model(self, name):
        if name == self.config["model"]:
            return
        self.config["model"] = name
        save_config(self.config)
        if name != self.worker.model_name:
            self.busy += 1
        self._refresh_status()
        self.worker.request_model(name)

    def _on_language(self, label):
        self.config["language"] = dict(LANGUAGE_CHOICES).get(label, "")
        save_config(self.config)

    def _on_mic(self, label):
        self.config["input_device"] = dict(self.mic_choices).get(label, -1)
        save_config(self.config)

    def _on_enter_toggle(self):
        self.config["press_enter"] = bool(self.enter_var.get())
        save_config(self.config)

    def _on_hotkey(self, name):
        self.config["hotkey"] = name
        save_config(self.config)
        self._sync_hotkey_button()
        self._refresh_status()

    # -- main loop ---------------------------------------------------------
    def _poll(self):
        try:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:
                    self._log(f"[internal error] {exc}")
        finally:
            if self.recording:
                self.level.set(min(1.0, self.recorder.level * 12))
            self.root.after(40, self._poll)

    def _handle_event(self, kind, payload):
        if kind == "press":
            if not self.recording:
                self._start_recording()
        elif kind == "release":
            if self.recording:
                self._stop_recording()
        elif kind == "status":
            state, message = payload
            if state == "busy":
                self.busy_text = message
            self._refresh_status()
        elif kind == "loaded":
            self.busy = max(0, self.busy - 1)
            self.busy_text = "Working..."
            self._refresh_status()
        elif kind == "text":
            self.busy = max(0, self.busy - 1)
            self.busy_text = "Working..."
            self._refresh_status()
            if payload:
                self._log(payload)
                try:
                    paste_text(payload, self.enter_var.get())
                except Exception as exc:
                    self._log(f"[typing error] {exc}")
        elif kind == "error":
            self.busy = max(0, self.busy - 1)
            self.busy_text = "Working..."
            self._log(f"[error] {payload}")
            self._refresh_status()
        elif kind == "hotkey":
            self._on_hotkey(payload)

    def _on_close(self):
        try:
            self.recorder.abort()
            self.listener.stop()
        except Exception:
            pass
        save_config(self.config)
        self.root.destroy()


# --------------------------------------------------------------------------- entry point

_mutex = None


def already_running():
    """Keep a second copy from double-typing everything."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    _mutex = kernel32.CreateMutexW(None, False, "Local\\WisperSingleInstance")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main():
    if already_running():
        root = ctk.CTk()
        root.withdraw()
        messagebox.showinfo("Wisper", "Wisper is already running.")
        root.destroy()
        return
    ctk.set_default_color_theme("blue")
    ctk.set_appearance_mode(load_config()["appearance"])
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
