# Verification — 11 September 2026

Checks ran on a disposable copy, with no personal credentials and external provider calls disabled.

- PASS: Local UI/API response 200 without credentials.
- PASS: Foreign Host, cross-origin browser request and non-loopback client rejected (403).
- PASS: Actual FFmpeg split: six-second synthetic video -> three MP4 segments.

These checks do not prove that every AI model, video platform, voice or hardware configuration works. Full provider workflows and model-heavy processing require separate configured runs. Credential handling is documented in [SECURITY.md](SECURITY.md).
