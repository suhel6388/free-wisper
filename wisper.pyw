"""Wisper - hold a key, speak, and the text gets typed wherever your cursor is.

Push-to-talk dictation using faster-whisper running locally on this PC.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import ttk

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
}

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
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except Exception:
        pass
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
                        vad_filter=False,
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
        name = getattr(self, "pending_name", "model")
        if total:
            percent = int(done * 100 / total)
            text = f"Downloading {name}: {percent}% ({done / mb:.0f}/{total / mb:.0f} MB)"
        else:
            text = f"Downloading {name}: {done / mb:.0f} MB"
        self.events.put(("status", ("busy", text)))


# --------------------------------------------------------------------------- app

class App:
    STATUS_STYLES = {
        "idle": ("Hold {key} and speak", "#eef1f5", "#3d4451"),
        "recording": ("Recording - release to type", "#fde8e8", "#a02020"),
        "busy": ("Working...", "#fff4d6", "#8a5a00"),
        "error": ("Error - see below", "#fde8e8", "#a02020"),
    }
    def __init__(self, root):
        self.root = root
        self.config = load_config()
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

    # -- ui ----------------------------------------------------------------
    def _build_ui(self):
        root = self.root
        root.title("Wisper")
        root.geometry("460x600")
        root.minsize(420, 520)
        root.configure(bg="#f4f6f8")

        header = tk.Frame(root, bg="#f4f6f8")
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="Wisper", font=("Segoe UI", 18, "bold"),
                 bg="#f4f6f8", fg="#1c1f26").pack(anchor="w")
        tk.Label(header, text="Local voice typing - press, speak, release.",
                 font=("Segoe UI", 9), bg="#f4f6f8", fg="#6b7280").pack(anchor="w")

        card = tk.Frame(root, bg="white", highlightthickness=1,
                        highlightbackground="#e2e6ec")
        card.pack(fill="x", padx=16, pady=(6, 10))
        self.status = tk.Label(card, text="", font=("Segoe UI", 12, "bold"),
                               bg="#eef1f5", fg="#3d4451", pady=16)
        self.status.pack(fill="x")
        self.level = ttk.Progressbar(card, maximum=1.0, value=0.0)
        self.level.pack(fill="x", padx=1, pady=(1, 1))
        self.hint = tk.Label(card, text="", font=("Segoe UI", 9), bg="white",
                             fg="#6b7280", anchor="w", justify="left", pady=8,
                             wraplength=400)
        self.hint.pack(fill="x", padx=12)

        settings = ttk.LabelFrame(root, text="Settings")
        settings.pack(fill="x", padx=16, pady=(0, 10))
        settings.columnconfigure(1, weight=1)

        def row(r, label):
            ttk.Label(settings, text=label).grid(row=r, column=0, sticky="w",
                                                 padx=(10, 8), pady=4)

        row(0, "Model")
        self.model_var = tk.StringVar(value=self.config["model"])
        self.model_box = ttk.Combobox(settings, textvariable=self.model_var,
                                      values=MODEL_CHOICES, state="readonly")
        self.model_box.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=4)
        self.model_box.bind("<<ComboboxSelected>>", self._on_model)

        row(1, "Language")
        self.language_var = tk.StringVar(
            value=next((l for l, c in LANGUAGE_CHOICES if c == self.config["language"]), "English"))
        self.language_box = ttk.Combobox(settings, textvariable=self.language_var,
                                         values=[l for l, _ in LANGUAGE_CHOICES],
                                         state="readonly")
        self.language_box.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=4)
        self.language_box.bind("<<ComboboxSelected>>", self._on_language)

        row(2, "Microphone")
        labels = [label for label, _ in self.mic_choices]
        current = self.config["input_device"]
        self.mic_var = tk.StringVar(
            value=next((label for label, idx in self.mic_choices if idx == current),
                       labels[0]))
        self.mic_box = ttk.Combobox(settings, textvariable=self.mic_var,
                                    values=labels, state="readonly")
        self.mic_box.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=4)
        self.mic_box.bind("<<ComboboxSelected>>", self._on_mic)

        # Scrolling over a closed dropdown should not change the setting.
        for box in (self.model_box, self.language_box, self.mic_box):
            box.bind("<MouseWheel>", lambda _event: "break")

        row(3, "Push to talk")
        self.hotkey_btn = ttk.Button(settings, text="", command=self._begin_hotkey_capture)
        self.hotkey_btn.grid(row=3, column=1, sticky="ew", padx=(0, 10), pady=4)

        row(4, "")
        self.enter_var = tk.BooleanVar(value=bool(self.config["press_enter"]))
        ttk.Checkbutton(settings, text="Press Enter after typing",
                        variable=self.enter_var,
                        command=self._on_enter_toggle).grid(
            row=4, column=1, sticky="w", padx=(0, 10), pady=(2, 8))

        history = ttk.LabelFrame(root, text="Transcriptions")
        history.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.log = tk.Text(history, height=8, wrap="word", relief="flat",
                           bg="white", fg="#1c1f26", font=("Segoe UI", 10),
                           padx=8, pady=6, state="disabled")
        scroll = ttk.Scrollbar(history, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", padx=(0, 6), pady=6)

        self._sync_hotkey_button()
        self.hint.configure(text=self._model_hint())

    def _model_hint(self):
        return ("Runs on your CPU. base.en is a good balance; small.en is more accurate "
                "but slower. A new model downloads once (~75 MB tiny, ~145 MB base, "
                "~480 MB small).")

    def _sync_hotkey_button(self):
        label = "Press any key..." if self.capturing else pretty_key(self.config["hotkey"])
        self.hotkey_btn.configure(text=f"Change  ({label})")

    # -- status ------------------------------------------------------------
    def _refresh_status(self):
        if self.capturing:
            text, bg, fg = "Press a key to use for push-to-talk", "#e8eefc", "#1d4ed8"
        elif self.recording:
            text, bg, fg = self.STATUS_STYLES["recording"]
        elif self.busy > 0:
            _template, bg, fg = self.STATUS_STYLES["busy"]
            text = self.busy_text
        else:
            template, bg, fg = self.STATUS_STYLES["idle"]
            text = template.format(key=pretty_key(self.config["hotkey"]))
        self.status.configure(text=text, bg=bg, fg=fg)
        if not self.recording:
            self.level["value"] = 0.0

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
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
        self.level["value"] = 0.0
        if self.shortcut_used:
            self.shortcut_used = False
            self._refresh_status()
            return
        if seconds < MIN_SECONDS or peak < MIN_LEVEL:
            self._refresh_status()
            return
        self.busy += 1
        language = dict(LANGUAGE_CHOICES).get(self.language_var.get(), "")
        self.worker.request_transcribe(self.config["model"], audio, language)
        self._refresh_status()

    # -- settings ----------------------------------------------------------
    def _on_model(self, _event=None):
        name = self.model_var.get()
        if name == self.config["model"]:
            return
        self.config["model"] = name
        save_config(self.config)
        if name != self.worker.model_name:
            self.busy += 1
        self._refresh_status()
        self.worker.request_model(name)

    def _on_language(self, _event=None):
        self.config["language"] = dict(LANGUAGE_CHOICES)[self.language_var.get()]
        save_config(self.config)

    def _on_mic(self, _event=None):
        mapping = dict(self.mic_choices)
        self.config["input_device"] = mapping.get(self.mic_var.get(), -1)
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
                self.level["value"] = min(1.0, self.recorder.level * 12)
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
        root = tk.Tk()
        root.withdraw()
        from tkinter import messagebox

        messagebox.showinfo("Wisper", "Wisper is already running.")
        root.destroy()
        return
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
