# Limitations and integration requirements

Duration-based splitting produced three playable MP4 files. Optional network downloading and neural audio separation were not part of that check.

Install FFmpeg and ffprobe on PATH. Core splitting needs no API key. Optional neural audio separation requires the model dependencies from `requirements.txt`.

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits.
