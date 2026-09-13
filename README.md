# Video & Audio Splitter

Video & Audio Splitter divides long recordings into shorter, playable files. Use duration limits or explicit segment boundaries, adjust encoding settings, and track the exported results through a local web interface.

## Features

- Split by explicit segments or automatic duration limits.
- Control encoding quality and segment boundaries.
- Inspect progress and browse exported results.
- Use optional command-line audio separation when configured.

## How it works

The web layer translates user settings into a reusable command-line media processing engine. FFmpeg and ffprobe handle cutting and inspection.

**Stack:** Python · Flask · FFmpeg · yt-dlp

## Getting started

Use Python 3.12 and a separate virtual environment. Run the following commands from this repository's root in Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

Install FFmpeg and ffprobe on PATH. Core splitting needs no API key. Optional neural audio separation requires the model dependencies from `requirements.txt`.

### Start the application

Use the local URL printed in the terminal. FFmpeg and ffprobe must be on PATH. Core splitting needs no API key. Optional Demucs features require a separate ML installation.

```powershell
python web_app.py
```

## Example workflow

Split a six-second recording into three two-second clips and inspect each exported file.

## Testing and limitations

Duration-based splitting produced three playable MP4 files. Optional network downloading and neural audio separation were not part of that check.

See [Verification](VERIFICATION.md) for the recorded checks and [Limitations](LIMITATIONS.md) for integration requirements.

## Configuration and security

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits. Configure your own provider credentials when a feature requires them; credentials and personal data are not included. See [Security](SECURITY.md) for local configuration and reporting guidance.
