# Install and Use Wisper

Wisper is a Windows push-to-talk dictation app. It uses a local speech model, so no
audio is uploaded.

## Requirements

- Windows 10 or Windows 11
- Python 3.12
- An internet connection for the first model download
- A microphone

Install Python 3.12 from a terminal:

```powershell
winget install Python.Python.3.12
```

Restart the terminal or sign out and back in if `py -3.12` is not found.

## Installation

1. Download or clone this project.
2. Open the project folder.
3. Double-click `setup.bat`.
4. Wait for the Python environment and dependencies to finish installing.

`setup.bat` creates a local `.venv` folder and installs the packages in
`requirements.txt`.

## Start Wisper

Double-click `run.bat`.

The app can also be started from PowerShell:

```powershell
.\.venv\Scripts\pythonw.exe .\wisper.pyw
```

## Use Wisper

1. Place the cursor in a text field.
2. Hold **Left Ctrl** and speak.
3. Release the key.
4. Wisper transcribes the recording and pastes the text at the cursor.

The app window shows microphone activity and recent transcription status.

### Configure Wisper

Open the Wisper window and adjust:

- **Model:** `base.en` is the default. `tiny.en` is faster and smaller; `small.en`,
  `medium.en`, and larger models are more accurate but require more CPU and disk space.
- **Language:** English or automatic language detection.
- **Microphone:** The system default or a specific connected microphone.
- **Push to talk:** Click the key control and press any key to change the recording key.
- **Press Enter after typing:** Add Enter after inserted text.

Settings are stored in `config.json`.

## Models

The selected model downloads automatically on first use. Files are stored in
`models/<model-name>/`.

Approximate starting sizes:

| Model | Size |
| --- | --- |
| `tiny.en` | About 75 MB |
| `base.en` | About 145 MB |
| `small.en` | About 480 MB |

Downloads resume if interrupted.

## Troubleshooting

### Setup fails

Use Python 3.12 and run `setup.bat` again. `ctranslate2` and `av` may not provide
wheels for newer Python releases.

### No text is inserted

- Confirm the microphone is working and that Wisper has permission to use it.
- Place the cursor in a text field that accepts pasted text.
- Hold the key long enough: recordings shorter than 0.35 seconds are ignored.
- Some applications block clipboard paste; try a text editor such as Notepad.

### The model download is interrupted

Press the push-to-talk key again. Wisper retries the download and resumes partial
files.

### Microphone is not found

Select the microphone explicitly in the app window. If it does not appear, confirm
that Windows detects it and enable it in Windows microphone privacy settings.
