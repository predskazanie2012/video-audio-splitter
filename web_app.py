#!/usr/bin/env python3
"""Local web UI: file / folder pick / YouTube, async job + log panel, optional copy to folder."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_from_directory, url_for

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "video_audio_split.py"
UPLOAD = ROOT / "web_uploads"
OUTPUT = ROOT / "web_output"
PORT = int(os.environ.get("VIDEO_AUDIO_SPLIT_PORT", "7845"))
DEFAULT_RESULT_FOLDER = Path(__file__).resolve().parent / "output"
DEFAULT_VIDEO_FOLDER = DEFAULT_RESULT_FOLDER
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"}

UPLOAD.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

from local_access import protect_flask
protect_flask(app)

app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024

JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
MAX_LOG_LINES = 600


def _py() -> str:
    return sys.executable


def _job_log(jid: str, line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    text = f"[{ts}] {line}".rstrip()[:4000]
    with JOB_LOCK:
        if jid not in JOBS:
            return
        st = JOBS[jid]
        st.setdefault("logs", []).append(text)
        if len(st["logs"]) > MAX_LOG_LINES:
            st["logs"] = st["logs"][-MAX_LOG_LINES:]


def _copy_results_to_user_folder(odir: Path, raw_folder: str) -> tuple[Path | None, str | None]:
    s = (raw_folder or "").strip().strip('"').strip("'")
    if not s:
        return None, None
    try:
        dest = Path(s).expanduser().resolve()
    except (OSError, ValueError) as e:
        return None, f"Некорректный путь: {e}"
    if not dest.is_absolute():
        return None, "Укажите полный путь к папке (например D:\\Video\\out или F:/Videos/out)."
    try:
        if dest.resolve() == odir.resolve():
            return None, "Укажите другую папку: совпадает с рабочей папкой этого задания."
    except OSError:
        pass
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"Не удалось создать папку: {e}"
    try:
        for p in odir.iterdir():
            if p.is_file():
                shutil.copy2(p, dest / p.name)
    except OSError as e:
        return None, f"Ошибка копирования в папку: {e}"
    return dest, None


def _yt_dlp_ok() -> bool:
    try:
        r = subprocess.run(
            [_py(), "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _yt_dlp_cookies_opts() -> list[str]:
    path = ROOT / "cookies" / "youtube.txt"
    if path.is_file() and path.stat().st_size > 0:
        return ["--cookies", str(path)]
    return []


def _yt_dlp_youtube_opts() -> list[str]:
    """Флаги для стабильного скачивания YouTube.

    --js-runtimes node:node      решает n-challenge (Node.js v18+ обязателен)
    --remote-components ejs:github  загружает скрипт решателя с GitHub
    --force-ipv4                 предотвращает 403 при IPv4/IPv6 конфликте
    --extractor-args hls         открывает HLS-форматы (не требуют PO-токена)
    """
    return [
        "--js-runtimes", "node:node",
        "--remote-components", "ejs:github",
        "--force-ipv4",
        "--extractor-args", "youtube:formats=hls",
    ]


def _yt_dlp_network_options() -> tuple[list[str], float, int]:
    """Опции устойчивости к таймаутам CDN (googlevideo и т.д.)."""
    raw_sock = (os.environ.get("VIDEO_AUDIO_SPLIT_YTDLP_SOCKET_TIMEOUT") or "").strip()
    try:
        sock = float(raw_sock.replace(",", ".")) if raw_sock else 120.0
    except ValueError:
        sock = 120.0
    sock = max(20.0, min(600.0, sock))

    raw_r = (os.environ.get("VIDEO_AUDIO_SPLIT_YTDLP_RETRIES") or "").strip()
    try:
        retries = int(raw_r) if raw_r else 25
    except ValueError:
        retries = 25
    retries = max(5, min(50, retries))

    opts: list[str] = [
        "--socket-timeout",
        str(sock),
        "--retries",
        str(retries),
        "--fragment-retries",
        str(retries),
        "--extractor-retries",
        str(min(8, max(3, retries // 3))),
        "--retry-sleep",
        "http:exp=1:12",
        "--retry-sleep",
        "fragment:exp=1:20",
    ]
    return opts, sock, retries


def _is_youtube_url(url: str) -> bool:
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    h = (p.netloc or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h in ("youtube.com", "m.youtube.com", "youtu.be") or h.endswith(".youtube.com")


def _download_youtube(url: str, out_dir: Path, log_fn, timeout_sec: int = 14400) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "source.%(ext)s")
    net_opts, sock, retries = _yt_dlp_network_options()
    cmd = [
        _py(), "-m", "yt_dlp",
        *_yt_dlp_youtube_opts(),
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/96/95/94/93/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-warnings",
        *_yt_dlp_cookies_opts(),
        *net_opts,
        "-o", template,
        url.strip(),
    ]
    log_fn(
        f"yt-dlp: таймаут сокета {sock:g}s, повторов {retries} "
        f"(можно задать VIDEO_AUDIO_SPLIT_YTDLP_SOCKET_TIMEOUT и VIDEO_AUDIO_SPLIT_YTDLP_RETRIES)."
    )
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(out_dir),
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"yt-dlp exit {r.returncode}"
        raise RuntimeError(msg[-12000:])
    vids = []
    for p in out_dir.glob("source.*"):
        if p.name.endswith((".part", ".ytdl", ".temp")):
            continue
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4a"}:
            if p.suffix.lower() == ".m4a":
                continue
            vids.append(p)
    if not vids:
        raise RuntimeError("Скачивание завершилось, но видеофайл не найден.")
    mp4s = [p for p in vids if p.suffix.lower() == ".mp4"]
    return mp4s[0] if mp4s else max(vids, key=lambda x: x.stat().st_size)


def _env_float(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def _demucs_chunk_seconds_from_env() -> float | None:
    return _env_float("VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_SECONDS")


def _demucs_chunk_overlap_from_env() -> float | None:
    return _env_float("VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_OVERLAP")


def _ffprobe_duration(path: Path) -> float | None:
    probe = shutil.which("ffprobe")
    if not probe or not path.is_file():
        return None
    r = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        return None
    try:
        v = float((r.stdout or "").strip())
        return v if v > 0.2 else None
    except ValueError:
        return None


_RE_FFMPEG_HMS = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_FFMPEG_SEC = re.compile(r"time=(\d+\.\d+|\d+)(?=\s|bitrate=|$)")


def _parse_ffmpeg_time_sec(line: str) -> float | None:
    m = _RE_FFMPEG_HMS.search(line)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return float(h * 3600 + mi * 60 + s)
    m2 = _RE_FFMPEG_SEC.search(line)
    if m2:
        try:
            v = float(m2.group(1))
            return v if 0 <= v < 864000 else None
        except ValueError:
            return None
    return None


def _estimate_rough_total_sec(dur: float | None, size_bytes: int, audio_mode: str) -> float:
    """Грубая оценка времени пайплайна (сек), для ETA без детального прогресса."""
    if dur and dur > 0:
        mul = 4.0 if audio_mode in ("vocals", "vocals_music") else 2.0
        if audio_mode == "vocals_music":
            mul += 0.5
        return max(60.0, dur * mul)
    mb = max(1.0, size_bytes / (1024 * 1024))
    return max(120.0, mb * 45.0)


def _calc_progress_pct_eta(
    max_src_sec: float,
    vid_dur: float | None,
    elapsed_main: float,
    rough: float,
) -> tuple[float, float | None]:
    """Процент (2–94) и ETA по выводу ffmpeg / эвристике."""
    pct = 8.0
    eta: float | None = None
    if vid_dur and vid_dur > 0.5 and max_src_sec > 0.5:
        r = min(1.0, max_src_sec / vid_dur)
        pct = 8.0 + r * 82.0
        if r > 0.04 and elapsed_main > 1.5:
            rate = max_src_sec / elapsed_main
            if rate > 0.02:
                eta = max(0.0, (vid_dur - max_src_sec) / rate)
    else:
        pct = 8.0 + min(78.0, (elapsed_main / max(45.0, rough)) * 78.0)
        eta = max(0.0, rough - elapsed_main * 0.9)
    pct = min(94.0, max(2.0, pct))
    if eta is not None:
        eta = min(max(0.0, eta), rough * 5.0)
    return pct, eta


def _patch_job_progress(jid: str, **kwargs: Any) -> None:
    with JOB_LOCK:
        if jid not in JOBS:
            return
        pr = JOBS[jid].setdefault("progress", {})
        pr.update(kwargs)


def _worker(jid: str, spec: dict[str, Any]) -> None:
    log = lambda m: _job_log(jid, m)
    udir = Path(spec["udir"])
    odir = Path(spec["odir"])
    try:
        with JOB_LOCK:
            JOBS[jid]["status"] = "running"
        log("Старт задания.")
        _patch_job_progress(
            jid,
            phase="init",
            mode="indeterminate",
            label="Подготовка…",
            pct=1.0,
            eta_sec=None,
        )

        in_path: Path
        if spec.get("youtube_url"):
            _patch_job_progress(
                jid,
                phase="youtube",
                mode="indeterminate",
                label="Скачивание YouTube…",
                pct=2.0,
            )
            log("Скачивание с YouTube (может занять время)…")
            in_path = _download_youtube(spec["youtube_url"], udir, log)
            log(f"Видео сохранено: {in_path.name}")
        else:
            in_path = Path(spec["video_path"])
            if not in_path.is_file():
                raise RuntimeError(f"Нет входного файла: {in_path}")

        sz = in_path.stat().st_size if in_path.is_file() else 0
        vid_dur = _ffprobe_duration(in_path)
        rough = _estimate_rough_total_sec(vid_dur, sz, spec["audio_mode"])
        _patch_job_progress(
            jid,
            phase="probe",
            mode="determinate" if vid_dur else "indeterminate",
            label="Оценка длительности…",
            pct=6.0,
            rough_total_sec=round(rough),
            vid_duration_sec=round(vid_dur, 2) if vid_dur else None,
            eta_sec=max(30.0, rough * 0.9),
        )
        if vid_dur:
            log(f"Длительность по ffprobe: {vid_dur:.1f} с (~{vid_dur / 60:.1f} мин).")
        else:
            log("Длительность ffprobe не получена — ETA по грубой оценке.")

        cmd = [
            _py(),
            str(SCRIPT),
            str(in_path),
            "-o",
            str(odir),
            "--auto",
            "--max-duration",
            spec["max_d"],
            "--audio",
            spec["audio_mode"],
        ]
        if spec.get("music_path"):
            cmd.extend(["--music", spec["music_path"]])
        if spec["audio_mode"] == "vocals_music":
            cmd.extend(["--music-mix-volume", str(spec["music_mix"])])
        if spec.get("demucs_python"):
            cmd.extend(["--demucs-python", spec["demucs_python"]])
        if spec.get("demucs_chunk_seconds") is not None:
            cmd.extend(["--demucs-chunk-seconds", str(spec["demucs_chunk_seconds"])])
        if spec.get("demucs_chunk_overlap") is not None:
            cmd.extend(["--demucs-chunk-overlap", str(spec["demucs_chunk_overlap"])])

        log("Запуск нарезки (ниже строки из ffmpeg / скрипта)…")
        _patch_job_progress(
            jid,
            phase="main",
            label="Обработка и нарезка…",
            mode="determinate" if vid_dur else "indeterminate",
            pct=8.0,
        )

        t_main = time.monotonic()
        last_ui = 0.0
        max_src_t = 0.0
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(line)
                low = line.lower()
                if "demucs" in low:
                    _patch_job_progress(
                        jid,
                        phase="demucs",
                        label="Demucs (вокал)…",
                    )
                tsec = _parse_ffmpeg_time_sec(line)
                if tsec is not None:
                    max_src_t = max(max_src_t, tsec)
            now = time.monotonic()
            if now - last_ui > 0.45:
                last_ui = now
                em = now - t_main
                pct, eta = _calc_progress_pct_eta(max_src_t, vid_dur, em, rough)
                _patch_job_progress(
                    jid,
                    pct=round(pct, 1),
                    eta_sec=round(eta) if eta is not None else None,
                    elapsed_main_sec=round(em, 1),
                    mode="determinate" if (vid_dur or em > 5) else "indeterminate",
                )
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Код выхода {proc.returncode}")

        files = sorted(p.name for p in odir.iterdir() if p.is_file())
        if not files:
            raise RuntimeError("Нарезка завершилась, но выходных файлов нет.")
        log(f"Готово, файлов: {len(files)}")
        _patch_job_progress(jid, phase="finalize", label="Финализация…", pct=95.0, eta_sec=5)

        copy_dest, copy_err = _copy_results_to_user_folder(odir, spec.get("result_folder") or "")
        if copy_err:
            raise RuntimeError(copy_err)
        if copy_dest:
            log(f"Копия в папку: {copy_dest}")

        with JOB_LOCK:
            JOBS[jid]["status"] = "done"
            JOBS[jid]["files"] = files
            JOBS[jid]["copy_dest"] = str(copy_dest) if copy_dest else None
            JOBS[jid]["error"] = None
            pr = JOBS[jid].setdefault("progress", {})
            pr.update(
                {
                    "pct": 100.0,
                    "eta_sec": 0,
                    "phase": "done",
                    "mode": "determinate",
                    "label": "Готово",
                }
            )
            JOBS[jid]["progress"] = pr
    except subprocess.TimeoutExpired:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = "Превышено время ожидания (YouTube)."
        log("Ошибка: timeout.")
        _patch_job_progress(jid, phase="error", label="Ошибка", mode="indeterminate")
    except Exception as e:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)
        log("Ошибка: " + str(e))
        _patch_job_progress(jid, phase="error", label="Ошибка", mode="indeterminate")


@app.route("/")
def index():
    return render_template("index.html", err=None, default_result_folder=str(DEFAULT_RESULT_FOLDER))


@app.post("/api/pick-result-folder")
def pick_result_folder():
    raw_current = (request.form.get("current") or "").strip().strip('"').strip("'")
    initial = DEFAULT_RESULT_FOLDER
    if raw_current:
        try:
            current = Path(raw_current).expanduser()
            initial = current if current.is_dir() else current.parent
        except (OSError, ValueError):
            initial = DEFAULT_RESULT_FOLDER
    if not initial.is_dir():
        initial = DEFAULT_RESULT_FOLDER if DEFAULT_RESULT_FOLDER.parent.is_dir() else ROOT

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Выберите папку для готовых файлов",
            initialdir=str(initial),
            mustexist=False,
        )
        root.destroy()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Не удалось открыть выбор папки: {e}"}), 500

    return jsonify({"ok": True, "path": selected or ""})


@app.post("/api/pick-video-folder")
def pick_video_folder():
    raw_current = (request.form.get("current") or "").strip().strip('"').strip("'")
    initial = DEFAULT_VIDEO_FOLDER
    if raw_current:
        try:
            current = Path(raw_current).expanduser()
            initial = current if current.is_dir() else current.parent
        except (OSError, ValueError):
            initial = DEFAULT_VIDEO_FOLDER
    if not initial.is_dir():
        initial = DEFAULT_VIDEO_FOLDER if DEFAULT_VIDEO_FOLDER.parent.is_dir() else ROOT

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="Выберите папку с видео",
            initialdir=str(initial),
            mustexist=True,
        )
        root.destroy()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Не удалось открыть выбор папки: {e}"}), 500

    if not selected:
        return jsonify({"ok": True, "path": "", "files": []})

    folder = Path(selected)
    files = []
    try:
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({"name": p.name, "path": str(p.resolve())})
    except OSError as e:
        return jsonify({"ok": False, "error": f"Не удалось прочитать папку: {e}"}), 500

    return jsonify({"ok": True, "path": str(folder.resolve()), "files": files})


@app.post("/api/list-video-folder")
def list_video_folder():
    raw_folder = (request.form.get("folder") or "").strip().strip('"').strip("'")
    folder = DEFAULT_VIDEO_FOLDER
    if raw_folder:
        try:
            folder = Path(raw_folder).expanduser().resolve()
        except (OSError, ValueError) as e:
            return jsonify({"ok": False, "error": f"Некорректный путь к папке: {e}"}), 400
    if not folder.is_dir():
        return jsonify({"ok": False, "error": f"Папка не найдена: {folder}"}), 404

    files = []
    try:
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({"name": p.name, "path": str(p.resolve())})
    except OSError as e:
        return jsonify({"ok": False, "error": f"Не удалось прочитать папку: {e}"}), 500

    return jsonify({"ok": True, "path": str(folder), "files": files})


@app.post("/api/start")
def api_start():
    youtube_url = (request.form.get("youtube_url") or "").strip()
    f = request.files.get("video")
    source_tab = (request.form.get("source_tab") or "file").strip().lower()
    server_video_path_raw = (request.form.get("video_path_server") or "").strip()

    audio_mode = "copy"
    max_d = (request.form.get("max_duration") or "14:30").strip() or "14:30"
    result_folder_raw = request.form.get("result_folder") or ""

    def bad(msg: str):
        return jsonify({"ok": False, "error": msg}), 400

    use_youtube = source_tab == "youtube"
    if use_youtube:
        if not youtube_url:
            return bad("Вставьте ссылку на YouTube.")
        if not _yt_dlp_ok():
            return bad("Не найден yt-dlp (pip install yt-dlp).")
        if not _is_youtube_url(youtube_url):
            return bad("Разрешены только ссылки YouTube.")
    elif source_tab == "folder":
        if not server_video_path_raw:
            return bad("Выберите папку с видео и файл в списке.")
        try:
            server_video_path = Path(server_video_path_raw).expanduser().resolve()
        except (OSError, ValueError) as e:
            return bad(f"Некорректный путь к видео: {e}")
        if not server_video_path.is_file():
            return bad(f"Нет входного файла: {server_video_path}")
        if server_video_path.suffix.lower() not in VIDEO_EXTENSIONS:
            return bad("Выберите видеофайл.")
    else:
        if not f or not f.filename:
            return bad("Выберите видеофайл или вкладку «YouTube».")

    jid = secrets.token_hex(6)
    udir = UPLOAD / jid
    odir = OUTPUT / jid
    udir.mkdir(parents=True, exist_ok=True)
    odir.mkdir(parents=True, exist_ok=True)

    video_path_str: str | None = None
    if source_tab == "folder":
        video_path_str = str(server_video_path)
    elif not use_youtube:
        name = Path(f.filename).name
        if not name or name in (".", ".."):
            return bad("Некорректное имя видеофайла.")
        vp = udir / name
        f.save(vp)
        video_path_str = str(vp)

    spec: dict[str, Any] = {
        "jid": jid,
        "udir": str(udir),
        "odir": str(odir),
        "youtube_url": youtube_url if use_youtube else "",
        "video_path": video_path_str or "",
        "max_d": max_d,
        "audio_mode": audio_mode,
        "result_folder": result_folder_raw,
    }

    with JOB_LOCK:
        JOBS[jid] = {
            "status": "queued",
            "logs": [],
            "error": None,
            "files": [],
            "copy_dest": None,
            "started_mono": time.monotonic(),
            "progress": {
                "pct": 0.0,
                "eta_sec": None,
                "phase": "queued",
                "mode": "indeterminate",
                "label": "В очереди…",
                "rough_total_sec": None,
                "vid_duration_sec": None,
                "elapsed_main_sec": None,
            },
        }

    th = threading.Thread(target=_worker, args=(jid, spec), daemon=True)
    th.start()
    return jsonify({"ok": True, "job_id": jid})


@app.get("/api/status/<job_id>")
def api_status(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id or ""):
        return jsonify({"ok": False, "error": "bad id"}), 400
    with JOB_LOCK:
        st = JOBS.get(job_id)
    if not st:
        return jsonify({"ok": True, "status": "unknown", "logs": [], "error": None, "progress": None})
    pr = dict(st.get("progress") or {})
    t0 = st.get("started_mono")
    if t0 is not None:
        pr["elapsed_total_sec"] = round(time.monotonic() - t0, 1)
    return jsonify(
        {
            "ok": True,
            "status": st.get("status", "unknown"),
            "logs": st.get("logs", []),
            "error": st.get("error"),
            "files": st.get("files", []),
            "copy_dest": st.get("copy_dest"),
            "progress": pr,
        }
    )


@app.route("/done/<job_id>")
def job_done(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id or ""):
        abort(404)
    with JOB_LOCK:
        st = JOBS.get(job_id)
    if not st or st.get("status") != "done":
        return (
            render_template(
                "index.html",
                err="Задание не найдено, ещё выполняется или завершилось с ошибкой. Смотрите журнал на главной.",
            ),
            404,
        )
    files = st.get("files") or []
    return render_template(
        "result.html",
        job_id=job_id,
        files=files,
        copy_dest=st.get("copy_dest"),
    )


@app.post("/run")
def run_split_legacy():
    """Старый синхронный POST (без JS) — редирект на API не делаем; отдаём подсказку."""
    return (
        render_template(
            "index.html",
            err="Обновите страницу: нарезка запускается через кнопку «Нарезать» с журналом справа.",
        ),
        400,
    )


@app.route("/dl/<job_id>/<path:name>")
def download(job_id: str, name: str):
    if not job_id.isalnum() or len(job_id) > 32:
        abort(404)
    base = (OUTPUT / job_id).resolve()
    if not base.is_dir():
        abort(404)
    path = (base / name).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    return send_from_directory(str(base), path.name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)
