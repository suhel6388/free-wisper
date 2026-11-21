# Wisper

See [INSTALL.md](INSTALL.md) for complete installation and usage instructions.

Local push-to-talk dictation for Windows. Hold a key, speak, release — the text is
typed into whatever window has focus. Runs entirely on this PC with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper); nothing is uploaded.

## Setup (once)

1. Install Python 3.12 if you don't have it (`winget install Python.Python.3.12`).
2. Double-click `setup.bat`. It creates `.venv` and installs the dependencies.

## Use

Double-click `run.bat`.

- **Hold Left Ctrl** and speak. Release to transcribe and type it.
- The window shows the mic level and the last transcriptions.
- Settings (saved to `config.json`):
  - **Model** — `base.en` by default. `tiny.en` is fastest and smallest download,
    `small.en`/`medium.en` are more accurate but slower on CPU.
  - **Language** — English or auto-detect (auto needs a multilingual model, i.e. one
    without the `.en` suffix).
  - **Microphone** — pick a specific input or leave the system default.
  - **Push to talk** — click and press any key to change it (e.g. right Alt, F9).
  - **Press Enter after typing** — sends Enter after the text, for chat boxes.
- **Theme** — the button in the top-right cycles **☀ Light → ☾ Dark → ◐ System**.
  `System` follows the Windows light/dark setting and updates while the app runs.
  Your choice is saved in `config.json` and restored on the next launch.

The first use of a model downloads it into `models/<model-name>/` (~75 MB for
`tiny.en`, ~145 MB for `base.en`, ~480 MB for `small.en`). The status bar shows
progress, and an interrupted download resumes from where it stopped.

## Notes

- Holding Left Ctrl means Ctrl+shortcuts also start a recording. If another key is
  pressed while the key is held, that recording is discarded, so Ctrl+C / Ctrl+V keep
  working normally.
- Text is delivered via the clipboard (Ctrl+V) for speed and Unicode support; the
  previous clipboard text is restored afterwards.
- CPU only: `int8` on up to 8 threads.

## Files

| File | Purpose |
| --- | --- |
| `wisper.pyw` | The whole app (UI, hotkey, recording, transcription, typing). |
| `run.bat` | Launches it without a console window. |
| `setup.bat` | Creates the venv and installs dependencies. |
| `config.json` | Your settings, created on first run. |
| `models/` | Whisper models, one folder per model, downloaded by the app. |

## Troubleshooting

- **Nothing appears in the other window** — the cursor must be in a text field that
  accepts pasted text. Apps that block clipboard paste won't receive anything.
- **Download stops halfway** — it keeps retrying and resumes from the same byte; press
  the hotkey again to restart the attempt.
- **Setup fails on dependency wheels** — use Python 3.12 (`setup.bat` prefers it).
  Very new Python versions often have no prebuilt wheels for `ctranslate2`/`av`.
