#!/usr/bin/env python3
"""
Prepare video for YouTube: optional audio (copy / mute / replace / Demucs vocals / vocals+music),
split into segments (default <15 min).

Modes:
  --segments FILE.json  — ручная разметка (таймкоды + опционально YouTube-мета)
  --auto                — автонарезка под --max-duration; по умолчанию «умные» границы
                          (смены плана + тишина в аудио), иначе --no-smart-cut

Video: по умолчанию потоковое копирование (-c:v copy) — то же качество, что у исходника.
Для ровных стыков без артефактов ключевых кадров: --reencode-video (--crf / --preset).

Requires ffmpeg and ffprobe on PATH when splitting or probing.
Режимы vocals / vocals_music требуют pip install demucs (и PyTorch); на CPU долго на длинных роликах.
Demucs на длинном WAV держит в памяти тензор на всю длительность (~17 ГБ на ~3 ч); по умолчанию WAV режется на перекрывающиеся части и вокал стыкуется ffmpeg acrossfade (--demucs-chunk-seconds / --demucs-chunk-overlap).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def which_or_die(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.stderr.write(f"Не найден в PATH: {name}. Установите ffmpeg (включая ffprobe).\n")
        sys.exit(1)
    return path


def parse_time(s: str) -> float:
    """H:MM:SS, HH:MM:SS, M:SS, MM:SS -> seconds."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = int(parts[0]), float(parts[1])
        return m * 60 + sec
    if len(parts) == 3:
        h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + sec
    raise ValueError(f"Неверный формат времени: {s!r}")


def parse_max_duration(s: str) -> float:
    """Число секунд или строка времени (14:30, 1:00:00)."""
    s = str(s).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    return parse_time(s)


def fmt_clock(sec: float) -> str:
    """Человекочитаемое время для текстовых файлов."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(round(sec % 60))
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_ffmpeg_time(sec: float) -> str:
    if sec < 0:
        raise ValueError("duration negative")
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}".rstrip("0").rstrip(".")
    return f"{m}:{s:06.3f}".rstrip("0").rstrip(".")


def ffprobe_duration(ffprobe: str, path: Path) -> float:
    r = subprocess.run(
        [
            ffprobe,
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
        check=True,
    )
    return float(r.stdout.strip())


def parse_hashtags(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        tags = [str(x).strip().lstrip("#") for x in raw if str(x).strip()]
        return tags or None
    s = str(raw).strip()
    if not s:
        return None
    parts = re.split(r"[\s,;]+", s)
    return [p.strip().lstrip("#") for p in parts if p.strip()] or None


def hashtags_paste_line(tags: list[str] | None) -> str:
    if not tags:
        return ""
    return " ".join(f"#{t}" for t in tags)


@dataclass
class SegmentRow:
    name: str
    start: float
    end: float
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def has_youtube_meta(self) -> bool:
        return bool(
            (self.title and self.title.strip())
            or (self.description and self.description.strip())
            or (self.hashtags and len(self.hashtags) > 0)
        )


def load_segment_rows(path: Path) -> list[SegmentRow]:
    data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    out: list[SegmentRow] = []
    for i, row in enumerate(data):
        name = str(row.get("name", f"part{i+1:02d}"))
        start = parse_time(str(row["start"]))
        end = parse_time(str(row["end"]))
        if end <= start:
            raise ValueError(f"Сегмент {name}: end <= start")
        _warn_segment_too_long(name, end - start)
        title = row.get("title")
        desc = row.get("description")
        tags = parse_hashtags(row.get("hashtags"))
        out.append(
            SegmentRow(
                name=name,
                start=start,
                end=end,
                title=str(title).strip() if title is not None else None,
                description=str(desc).strip() if desc is not None else None,
                hashtags=tags,
            )
        )
    return out


def _warn_segment_too_long(name: str, dur_sec: float) -> None:
    if dur_sec > 900:
        sys.stderr.write(
            f"Предупреждение: {name} длиннее 15 мин ({dur_sec / 60:.2f} мин).\n"
        )


def _ffprobe_has_audio(ffprobe: str, path: Path) -> bool:
    r = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return "audio" in (r.stdout or "").lower()


def detect_scene_change_times(
    ffmpeg: str,
    path: Path,
    threshold: float,
) -> list[float]:
    """Таймкоды кадров со сменой сцены (lavfi scene)."""
    cmd = [
        ffmpeg,
        "-nostats",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-filter:v",
        f"select='gt(scene\\,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        sys.stderr.write(
            "Предупреждение: scene detection завершился с ошибкой; границы только по тишине/равномерно.\n"
        )
    times: list[float] = []
    for line in (r.stderr or "").splitlines():
        if "pts_time:" in line:
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                times.append(float(m.group(1)))
    return sorted(set(t for t in times if 0 < t))


def detect_silence_cut_times(
    ffmpeg: str,
    path: Path,
    noise_db: float,
    min_silence_sec: float,
) -> list[float]:
    """Точки внутри длинных пауз (середина и конец тишины) — удобно резать между фразами."""
    cmd = [
        ffmpeg,
        "-nostats",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f",
        "null",
        "-",
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        return []
    starts: list[float] = []
    ends: list[float] = []
    for line in (r.stderr or "").splitlines():
        ms = re.search(r"silence_start:\s*([\d.]+)", line)
        me = re.search(r"silence_end:\s*([\d.]+)", line)
        if ms:
            starts.append(float(ms.group(1)))
        if me:
            ends.append(float(me.group(1)))
    out: list[float] = []
    for s, e in zip(starts, ends):
        if e > s:
            out.append((s + e) / 2.0)
            out.append(e)
    return sorted(set(t for t in out if t > 0))


def merge_cut_candidates(
    duration: float,
    scenes: list[float],
    silences: list[float],
) -> list[float]:
    merged = sorted(set(scenes + silences))
    return [t for t in merged if 0 < t < duration]


def pick_cut_in_window(
    ideal: float,
    candidates: list[float],
    low: float,
    high: float,
) -> float:
    """Ближайший к ideal кандидат в [low, high]; иначе clamp(ideal)."""
    if high < low:
        low, high = high, low
    in_win = [c for c in candidates if low <= c <= high]
    if not in_win:
        return min(high, max(low, ideal))
    return min(in_win, key=lambda c: abs(c - ideal))


def build_smart_auto_segments(
    duration_sec: float,
    max_segment_sec: float,
    stem: str,
    candidates: list[float],
    window_sec: float,
    min_segment_sec: float,
) -> list[SegmentRow]:
    """
    n = ceil(D/max); границы близки к i*D/n, но сдвигаются к кандидатам (сцены/тишина)
    в допустимом интервале, чтобы ни один кусок не превышал max и не был короче min (где возможно).
    """
    if duration_sec <= 0:
        raise ValueError("Длительность видео должна быть > 0")
    if max_segment_sec <= 0:
        raise ValueError("--max-duration должен быть > 0")
    if min_segment_sec <= 0 or min_segment_sec > max_segment_sec:
        raise ValueError("Некорректный --min-segment относительно --max-duration")
    if max_segment_sec > 900:
        sys.stderr.write(
            "Предупреждение: --max-duration > 15 мин может не подойти для лимита YouTube.\n"
        )

    n = max(1, int(math.ceil(duration_sec / max_segment_sec)))
    width = max(2, len(str(n)))
    boundaries: list[float] = [0.0]

    for i in range(1, n):
        ideal = (i / n) * duration_sec
        prev = boundaries[-1]
        remaining_after = n - i
        search_low = max(
            prev + min_segment_sec,
            duration_sec - remaining_after * max_segment_sec,
        )
        search_high = min(
            prev + max_segment_sec,
            duration_sec - remaining_after * min_segment_sec,
        )
        soft_low = ideal - window_sec
        soft_high = ideal + window_sec
        low = max(search_low, soft_low)
        high = min(search_high, soft_high)
        if low > high:
            low, high = search_low, search_high
            if low > high:
                cut = min(search_high, max(search_low, ideal))
                boundaries.append(cut)
                continue
        cut = pick_cut_in_window(ideal, candidates, low, high)
        boundaries.append(cut)

    boundaries.append(duration_sec)

    for _ in range(3):
        bad = False
        for i in range(1, n):
            if boundaries[i] <= boundaries[i - 1] + 1e-6:
                boundaries[i] = boundaries[i - 1] + min_segment_sec
                bad = True
        for i in range(n - 1, 0, -1):
            if boundaries[i] >= boundaries[i + 1] - 1e-6:
                boundaries[i] = boundaries[i + 1] - min_segment_sec
                bad = True
        if not bad:
            break
    boundaries[-1] = duration_sec

    for i in range(n):
        if boundaries[i + 1] - boundaries[i] > max_segment_sec + 0.05:
            sys.stderr.write(
                "Не удалось уложиться в --max-duration со «умными» границами; "
                "используется равномерная нарезка. Попробуйте --no-smart-cut или другой порог сцены.\n"
            )
            return build_auto_segments(duration_sec, max_segment_sec, stem)

    rows: list[SegmentRow] = []
    for i in range(n):
        start = boundaries[i]
        end = boundaries[i + 1]
        name = f"{stem}{i + 1:0{width}d}"
        rows.append(SegmentRow(name=name, start=start, end=end))
    return rows


def build_auto_segments(
    duration_sec: float,
    max_segment_sec: float,
    stem: str,
) -> list[SegmentRow]:
    """Равная по длительности нарезка; число частей = ceil(D / max), каждая <= max."""
    if duration_sec <= 0:
        raise ValueError("Длительность видео должна быть > 0")
    if max_segment_sec <= 0:
        raise ValueError("--max-duration должен быть > 0")
    if max_segment_sec > 900:
        sys.stderr.write(
            "Предупреждение: --max-duration > 15 мин может не подойти для лимита YouTube.\n"
        )

    n = max(1, int(math.ceil(duration_sec / max_segment_sec)))
    width = max(2, len(str(n)))
    rows: list[SegmentRow] = []
    for i in range(n):
        start = (i / n) * duration_sec
        end = duration_sec if i == n - 1 else ((i + 1) / n) * duration_sec
        name = f"{stem}{i + 1:0{width}d}"
        rows.append(SegmentRow(name=name, start=start, end=end))
    return rows


def apply_youtube_placeholders(rows: list[SegmentRow], video_stem: str) -> list[SegmentRow]:
    n = len(rows)
    out: list[SegmentRow] = []
    for i, seg in enumerate(rows):
        idx = i + 1
        title = f"{video_stem} | Часть {idx}/{n}"
        desc = (
            f"Фрагмент {idx} из {n} (автоматическая нарезка).\n"
            f"Таймкоды в исходном файле: {fmt_clock(seg.start)} — {fmt_clock(seg.end)} "
            f"(длительность ~{seg.duration / 60:.1f} мин)."
        )
        out.append(
            SegmentRow(
                name=seg.name,
                start=seg.start,
                end=seg.end,
                title=title,
                description=desc,
                hashtags=["видео", "нарезка"],
            )
        )
    return out


def fmt_export_timecode(sec: float) -> str:
    """Точный таймкод для JSON (допускает дробные секунды)."""
    return fmt_ffmpeg_time(float(sec))


def build_auto_plan_from_video(
    ffmpeg: str,
    ffprobe: str,
    video: Path,
    max_seg: float,
    stem: str,
    smart_cut: bool,
    window_sec: float,
    min_seg_sec: float,
    scene_threshold: float,
    silence_noise_db: float,
    silence_min_sec: float,
) -> list[SegmentRow]:
    """План нарезки для --auto (равномерно или со сдвигом к сценам/тишине)."""
    if not smart_cut:
        dur = ffprobe_duration(ffprobe, video)
        sys.stderr.write("Автонарезка: равномерные границы (--no-smart-cut).\n")
        return build_auto_segments(dur, max_seg, stem)

    dur = ffprobe_duration(ffprobe, video)
    scenes = detect_scene_change_times(ffmpeg, video, scene_threshold)
    sil_pts: list[float] = []
    if _ffprobe_has_audio(ffprobe, video):
        sil_pts = detect_silence_cut_times(
            ffmpeg, video, silence_noise_db, silence_min_sec
        )
    cand = merge_cut_candidates(dur, scenes, sil_pts)
    sys.stderr.write(
        f"Умная нарезка: смен плана ~{len(scenes)}, точек по паузам в звуке ~{len(sil_pts)}; "
        f"кандидатов для границ {len(cand)} (окно ±{window_sec:.0f}s вокруг равномерной разметки).\n"
    )
    return build_smart_auto_segments(
        dur, max_seg, stem, cand, window_sec, min_seg_sec
    )


def rows_to_json_list(rows: list[SegmentRow]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for seg in rows:
        d: dict[str, Any] = {
            "name": seg.name,
            "start": fmt_export_timecode(seg.start),
            "end": fmt_export_timecode(seg.end),
        }
        if seg.title:
            d["title"] = seg.title
        if seg.description:
            d["description"] = seg.description
        if seg.hashtags:
            d["hashtags"] = seg.hashtags
        payload.append(d)
    return payload


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd)
    if p.returncode != 0:
        sys.exit(p.returncode)


def _resolve_demucs_device(pref: str) -> str:
    if pref == "cpu":
        return "cpu"
    if pref == "cuda":
        return "cuda"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _extract_wav_for_demucs(ffmpeg: str, video: Path, wav_out: Path) -> None:
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        str(wav_out),
    ]
    run(cmd)


def _extract_wav_segment_from_wav(
    ffmpeg: str, wav_in: Path, wav_out: Path, start_sec: float, dur_sec: float
) -> None:
    """Вырезка куска из WAV (те же параметры, что у полного demucs_track)."""
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_in),
        "-ss",
        f"{max(0.0, start_sec):.6f}",
        "-t",
        f"{max(0.01, dur_sec):.6f}",
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        str(wav_out),
    ]
    run(cmd)


def _concat_wavs_with_ffmpeg(ffmpeg: str, wav_parts: list[Path], out_wav: Path) -> None:
    if not wav_parts:
        sys.stderr.write("Demucs: нет фрагментов вокала для склейки.\n")
        sys.exit(1)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if len(wav_parts) == 1:
        shutil.copy2(wav_parts[0], out_wav)
        return
    list_path = out_wav.parent / f"_vocals_concat_{secrets.token_hex(6)}.txt"
    try:
        with list_path.open("w", encoding="utf-8") as f:
            for p in wav_parts:
                ap = str(p.resolve())
                ap = ap.replace("'", "'\\''")
                if os.name == "nt":
                    ap = ap.replace("\\", "/")
                f.write(f"file '{ap}'\n")
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(out_wav),
        ]
        run(cmd)
    finally:
        list_path.unlink(missing_ok=True)


def _stitch_vocal_chunks(
    ffmpeg: str,
    ffprobe: str,
    wav_parts: list[Path],
    out_wav: Path,
    overlap_sec: float,
) -> None:
    """Стык фрагментов вокала: при overlap>0 — перекрывающиеся куски и acrossfade; иначе concat."""
    if not wav_parts:
        sys.stderr.write("Demucs: нет фрагментов вокала для склейки.\n")
        sys.exit(1)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if len(wav_parts) == 1:
        shutil.copy2(wav_parts[0], out_wav)
        return
    if overlap_sec <= 0:
        _concat_wavs_with_ffmpeg(ffmpeg, wav_parts, out_wav)
        return

    durs = [ffprobe_duration(ffprobe, p) for p in wav_parts]
    fades: list[float] = []
    for i in range(len(durs) - 1):
        fades.append(max(0.04, min(float(overlap_sec), durs[i], durs[i + 1])))

    fc_lines: list[str] = []
    prev = "[0:a]"
    for i in range(1, len(wav_parts)):
        d = fades[i - 1]
        last = i == len(wav_parts) - 1
        out_lab = "[aout]" if last else f"[xf{i}]"
        fc_lines.append(f"{prev}[{i}:a]acrossfade=d={d:.4f}:c1=tri:c2=tri{out_lab}")
        prev = out_lab
    fc = ";".join(fc_lines)

    script_path = out_wav.parent / f"_acrossfade_{secrets.token_hex(6)}.txt"
    try:
        script_path.write_text(fc, encoding="utf-8")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for p in wav_parts:
            cmd.extend(["-i", str(p)])
        cmd.extend(
            [
                "-filter_complex_script",
                str(script_path),
                "-map",
                "[aout]",
                "-c:a",
                "pcm_s16le",
                str(out_wav),
            ]
        )
        run(cmd)
    finally:
        script_path.unlink(missing_ok=True)


def _run_demucs_vocals(
    py_exe: str,
    wav_in: Path,
    demucs_out_parent: Path,
    model: str,
    device: str,
    *,
    extra_args: list[str] | None = None,
) -> Path:
    """Run demucs two-stems=vocals; return path to vocals.wav."""
    demucs_out_parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        py_exe,
        "-m",
        "demucs",
        "-d",
        device,
        "-n",
        model,
        "--two-stems",
        "vocals",
        "-o",
        str(demucs_out_parent),
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(str(wav_in))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()[-8000:] or f"demucs exit {p.returncode}"
        sys.stderr.write(tail + "\n")
        sys.exit(1)
    found = list(demucs_out_parent.rglob("vocals.wav"))
    if not found:
        sys.stderr.write("Demucs завершился, но vocals.wav не найден.\n")
        sys.exit(1)
    return max(found, key=lambda x: x.stat().st_mtime)


def _build_demucs_chunk_ranges(duration_sec: float, chunk_sec: float, overlap_sec: float) -> list[tuple[float, float]]:
    """Список (start, dur) для кусочной обработки Demucs."""
    if duration_sec <= 0:
        return []
    out: list[tuple[float, float]] = []
    start = 0.0
    stride = chunk_sec - overlap_sec if overlap_sec > 0 else chunk_sec
    while start < duration_sec - 0.01:
        part_dur = min(chunk_sec, duration_sec - start)
        out.append((start, part_dur))
        start += stride if overlap_sec > 0 else part_dur
    return out


def _run_demucs_vocals_maybe_chunked(
    ffmpeg: str,
    ffprobe: str,
    py_exe: str,
    wav_in: Path,
    demucs_parent: Path,
    model: str,
    device: str,
    chunk_sec: float,
    overlap_sec: float,
) -> Path:
    """
    Demucs при split держит полный тензор выхода на всю длину входа (4 стема × stereo × samples).
    На ~3 ч это ~17 ГБ RAM — режем WAV по времени с перекрытием overlap_sec и склеиваем вокал
    через ffmpeg acrossfade (без щелчков на стыках).
    chunk_sec <= 0 — один вызов Demucs (как раньше, риск OOM на длинных дорожках).
    """
    demucs_parent.mkdir(parents=True, exist_ok=True)
    dur = ffprobe_duration(ffprobe, wav_in)
    demucs_extras = ["--shifts", "0"]

    if chunk_sec <= 0:
        sys.stderr.write(
            f"Demucs: один проход, {dur:.1f} с (chunk отключён — на многочасовом аудио возможен OOM).\n"
        )
        return _run_demucs_vocals(
            py_exe, wav_in, demucs_parent, model, device, extra_args=demucs_extras
        )
    if dur <= chunk_sec + 0.05:
        sys.stderr.write(f"Demucs: один проход, {dur:.1f} с.\n")
        return _run_demucs_vocals(
            py_exe, wav_in, demucs_parent, model, device, extra_args=demucs_extras
        )

    ov = max(0.0, float(overlap_sec))
    if ov >= chunk_sec - 1e-6:
        sys.stderr.write(
            "Demucs: --demucs-chunk-overlap слишком большой относительно куска — стык без перекрытия по времени.\n"
        )
        ov = 0.0
    stride = chunk_sec - ov if ov > 0 else chunk_sec

    chunk_ranges = _build_demucs_chunk_ranges(dur, chunk_sec, ov)
    n_est = len(chunk_ranges)
    stitch_note = (
        f"стык acrossfade ~{ov:.1f} с"
        if ov > 0
        else "стык concat (перекрытие 0)"
    )
    sys.stderr.write(
        f"Demucs: {dur:.1f} с — ~{n_est} фрагментов по ≤{chunk_sec:.0f} с, шаг {stride:.1f} с "
        f"({stitch_note}, экономия RAM).\n"
    )
    chunk_work = demucs_parent / f"_chunks_{secrets.token_hex(4)}"
    chunk_work.mkdir(parents=True, exist_ok=True)
    vocal_parts: list[Path] = []
    try:
        for idx, (start, part_dur) in enumerate(chunk_ranges):
            sys.stderr.write(
                f"Demucs: фрагмент {idx + 1}/{n_est} ({start:.1f}–{start + part_dur:.1f} с)…\n"
            )
            cwav = chunk_work / f"in_{idx:04d}.wav"
            _extract_wav_segment_from_wav(ffmpeg, wav_in, cwav, start, part_dur)
            sep_dir = chunk_work / f"out_{idx:04d}"
            sep_dir.mkdir(parents=True, exist_ok=True)
            v = _run_demucs_vocals(
                py_exe, cwav, sep_dir, model, device, extra_args=demucs_extras
            )
            vocal_parts.append(v)
            try:
                cwav.unlink(missing_ok=True)
            except OSError:
                pass
        out_vocals = demucs_parent / "vocals_stitched.wav"
        _stitch_vocal_chunks(ffmpeg, ffprobe, vocal_parts, out_vocals, ov)
        return out_vocals
    finally:
        shutil.rmtree(chunk_work, ignore_errors=True)


def _mux_video_vocals(
    ffmpeg: str, video: Path, vocals_wav: Path, out: Path, audio_bitrate: str
) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(vocals_wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-shortest",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        str(out),
    ]
    run(cmd)


def _mux_video_vocals_plus_music(
    ffmpeg: str,
    ffprobe: str,
    video: Path,
    vocals_wav: Path,
    music: Path,
    out: Path,
    audio_bitrate: str,
    music_volume: float,
) -> None:
    if not music.is_file():
        sys.stderr.write("vocals_music: файл музыки не найден.\n")
        sys.exit(1)
    dur = ffprobe_duration(ffprobe, video)
    fade_out_start = max(0.0, dur - 3.0)
    mv = max(0.01, min(0.6, float(music_volume)))
    fc = (
        f"[2:a]volume={mv},afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_out_start:.3f}:d=3[m];"
        f"[1:a][m]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(vocals_wav),
        "-stream_loop",
        "-1",
        "-i",
        str(music),
        "-filter_complex",
        fc,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-t",
        f"{dur:.3f}",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        str(out),
    ]
    run(cmd)


def build_audio_filtered_video(
    ffmpeg: str,
    ffprobe: str,
    video: Path,
    out: Path,
    mode: str,
    music: Path | None,
    audio_bitrate: str,
    *,
    demucs_model: str = "htdemucs_ft",
    demucs_device: str = "auto",
    demucs_python: Path | None = None,
    music_mix_volume: float = 0.18,
    demucs_chunk_seconds: float = 480.0,
    demucs_chunk_overlap_sec: float = 3.0,
) -> None:
    """mode: none | replace | vocals | vocals_music. Writes out (temp) with -c:v copy."""

    if mode == "none":
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            str(out),
        ]
        run(cmd)
        return

    if mode in ("vocals", "vocals_music"):
        if importlib.util.find_spec("demucs") is None:
            sys.stderr.write(
                "Режим vocals*: установите demucs (например: pip install demucs).\n"
            )
            sys.exit(1)
        if not _ffprobe_has_audio(ffprobe, video):
            sys.stderr.write("В видео нет аудио — режим vocals* недоступен.\n")
            sys.exit(1)
        work = out.parent / f"_demucs_work_{secrets.token_hex(5)}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            wav = work / "demucs_track.wav"
            demucs_py = str(demucs_python.resolve()) if demucs_python else sys.executable
            if demucs_python and not Path(demucs_py).is_file():
                sys.stderr.write("Предупреждение: --demucs-python не найден, используется текущий Python.\n")
                demucs_py = sys.executable
            sys.stderr.write(
                f"Demucs ({demucs_model}, {_resolve_demucs_device(demucs_device)}, python={demucs_py}): извлечение вокала…\n"
            )
            _extract_wav_for_demucs(ffmpeg, video, wav)
            demucs_parent = work / "separated"
            voc = _run_demucs_vocals_maybe_chunked(
                ffmpeg,
                ffprobe,
                demucs_py,
                wav,
                demucs_parent,
                demucs_model,
                _resolve_demucs_device(demucs_device),
                demucs_chunk_seconds,
                demucs_chunk_overlap_sec,
            )
            if mode == "vocals":
                _mux_video_vocals(ffmpeg, video, voc, out, audio_bitrate)
            else:
                _mux_video_vocals_plus_music(
                    ffmpeg,
                    ffprobe,
                    video,
                    voc,
                    music,  # type: ignore[arg-type]
                    out,
                    audio_bitrate,
                    music_mix_volume,
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return

    if mode == "replace":
        if not music or not music.is_file():
            sys.stderr.write("Режим replace: укажите существующий --music\n")
            sys.exit(1)
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-stream_loop",
            "-1",
            "-i",
            str(music),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            str(out),
        ]
        run(cmd)
        return

    raise ValueError(mode)


def split_segment(
    ffmpeg: str,
    src: Path,
    seg: SegmentRow,
    out_file: Path,
    reencode_video: bool,
    video_crf: int,
    video_preset: str,
    fast_seek: bool,
) -> None:
    dur = seg.duration
    if dur <= 0:
        raise ValueError(seg)

    if fast_seek:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            fmt_ffmpeg_time(seg.start),
            "-i",
            str(src),
            "-t",
            fmt_ffmpeg_time(dur),
        ]
    else:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-ss",
            fmt_ffmpeg_time(seg.start),
            "-t",
            fmt_ffmpeg_time(dur),
        ]

    if reencode_video:
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-crf",
            str(video_crf),
            "-preset",
            video_preset,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]

    cmd.append(str(out_file))
    run(cmd)


def write_youtube_txt(
    path: Path,
    seg: SegmentRow,
    video_filename: str | None,
) -> None:
    lines: list[str] = []
    if video_filename:
        lines.append(f"ФАЙЛ ВИДЕО: {video_filename}")
    lines.append(
        f"ФРАГМЕНТ (таймкоды в полном фильме): {fmt_clock(seg.start)} — {fmt_clock(seg.end)}"
    )
    lines.append("")
    lines.append("НАЗВАНИЕ (Title):")
    lines.append(seg.title or "")
    lines.append("")
    lines.append("ОПИСАНИЕ (Description):")
    lines.append(seg.description or "")
    ht_line = hashtags_paste_line(seg.hashtags)
    if ht_line:
        lines.append("")
        lines.append("---")
        lines.append("ХЕШТЕГИ — строка для вставки в конец описания или в отдельное поле:")
        lines.append(ht_line)
        lines.append("")
        lines.append("СПИСОК ХЕШТЕГОВ (по одному в строке):")
        for t in seg.hashtags or []:
            lines.append(f"#{t}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_youtube_metadata_json(path: Path, rows: list[SegmentRow], video_suffix: str) -> None:
    payload = []
    for seg in rows:
        video_name = f"{seg.name}{video_suffix}"
        payload.append(
            {
                "name": seg.name,
                "video_file": video_name,
                "start_sec": seg.start,
                "end_sec": seg.end,
                "start_label": fmt_clock(seg.start),
                "end_label": fmt_clock(seg.end),
                "duration_sec": round(seg.duration, 3),
                "title": seg.title,
                "description": seg.description,
                "hashtags": seg.hashtags,
                "hashtags_line": hashtags_paste_line(seg.hashtags),
            }
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_all_youtube_sidecars(
    out_dir: Path,
    rows: list[SegmentRow],
    video_suffix: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for seg in rows:
        if not seg.has_youtube_meta():
            continue
        vname = f"{seg.name}{video_suffix}"
        write_youtube_txt(out_dir / f"{seg.name}_youtube.txt", seg, vname)
    meta_path = out_dir / "youtube_metadata.json"
    if any(s.has_youtube_meta() for s in rows):
        write_youtube_metadata_json(meta_path, rows, video_suffix)
        sys.stderr.write(f"Сводный JSON: {meta_path.name}\n")


def main() -> None:
    here = Path(__file__).resolve().parent
    default_json = here / "default_segments.json"

    ap = argparse.ArgumentParser(
        description="Нарезка видео до 15 минут (YouTube): JSON или --auto."
    )
    ap.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=None,
        help="Исходное видео (не нужно при --metadata-only без --auto)",
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        required=True,
        help="Папка для выходных файлов",
    )
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Автоматическая нарезка по длительности (см. --max-duration); не использует --segments",
    )
    ap.add_argument(
        "--max-duration",
        default="14:30",
        help="Макс. длина одного куска: секунды (число) или M:SS / H:MM:SS (по умолчанию 14:30)",
    )
    ap.add_argument(
        "--part-prefix",
        default="part",
        help="Префикс имён файлов при --auto (по умолчанию part -> part01, ...)",
    )
    ap.add_argument(
        "--no-smart-cut",
        action="store_true",
        help="При --auto резать только по времени (без поиска сцен и пауз в звуке)",
    )
    ap.add_argument(
        "--smart-cut-window",
        default="120",
        help="Насколько сдвигать границу от равномерной точки к сцене/тишине: секунды или M:SS (по умолчанию 120)",
    )
    ap.add_argument(
        "--min-segment",
        default="45",
        help="Мин. длина куска при умной нарезке, секунды или M:SS (по умолчанию 45)",
    )
    ap.add_argument(
        "--scene-threshold",
        type=float,
        default=0.32,
        help="Чувствительность детектора смены плана 0..1 (выше — меньше срабатываний)",
    )
    ap.add_argument(
        "--silence-noise-db",
        type=float,
        default=-35.0,
        help="Порог тишины silencedetect (дБ, обычно от -50 до -25)",
    )
    ap.add_argument(
        "--silence-min",
        type=float,
        default=0.4,
        help="Мин. длительность тишины (сек), чтобы считать паузой для резки",
    )
    ap.add_argument(
        "--youtube-placeholders",
        action="store_true",
        help="При --auto сгенерировать простые названия/описания/хештеги для YouTube",
    )
    ap.add_argument(
        "--write-segments-json",
        type=Path,
        help="Сохранить рассчитанные границы в JSON (удобно править вручную и потом --segments)",
    )
    ap.add_argument(
        "--metadata-only",
        action="store_true",
        help="Без нарезки видео: только тексты/JSON. С --auto нужен input (ffprobe + при умной нарезке анализ ffmpeg).",
    )
    ap.add_argument(
        "--audio",
        choices=("copy", "none", "replace", "vocals", "vocals_music"),
        default="copy",
        help="copy — как в файле; none — без звука; replace — полностью заменить на --music; "
        "vocals — Demucs: оставить вокал (фон/музыка сильно убраны, не идеально); "
        "vocals_music — Demucs + ваша музыка под голосом (--music, см. --music-mix-volume).",
    )
    ap.add_argument(
        "--music",
        type=Path,
        help="Файл музыки для режима replace (зацикливается на длину видео)",
    )
    ap.add_argument(
        "--segments",
        type=Path,
        default=None,
        help=f"JSON с разметкой (если не задан и нет --auto: {default_json})",
    )
    ap.add_argument(
        "--audio-bitrate",
        default="192k",
        help="Битрейт AAC при перекодировании аудио (replace / vocals*).",
    )
    ap.add_argument(
        "--demucs-model",
        default="htdemucs_ft",
        help="Модель Demucs для vocals|vocals_music (htdemucs, htdemucs_ft, …).",
    )
    ap.add_argument(
        "--demucs-device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="Устройство Demucs: auto — CUDA если доступна.",
    )
    ap.add_argument(
        "--demucs-python",
        type=Path,
        default=None,
        help="Другой python.exe для «python -m demucs», если в текущем venv torchaudio падает (WinError 127 на Windows). "
        "Иначе переменные VIDEO_AUDIO_SPLIT_DEMUCS_PYTHON или DEMUCS_PYTHON.",
    )
    ap.add_argument(
        "--demucs-chunk-seconds",
        type=float,
        default=480.0,
        help="Длинный WAV для Demucs режется на фрагменты ≤N с и вокал склеивается (иначе OOM: полный тензор на всю длительность). "
        "0 — один вызов Demucs. Переменная VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_SECONDS переопределяет значение.",
    )
    ap.add_argument(
        "--demucs-chunk-overlap",
        type=float,
        default=3.0,
        help="Перекрытие соседних фрагментов по исходному времени (сек) и плавный стык ffmpeg acrossfade. "
        "Должно быть меньше --demucs-chunk-seconds. 0 — жёсткий concat. Переменная VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_OVERLAP.",
    )
    ap.add_argument(
        "--music-mix-volume",
        type=float,
        default=0.18,
        help="Громкость фона 0.01–0.6 при --audio vocals_music (голос с Demucs).",
    )
    ap.add_argument(
        "--reencode-video",
        action="store_true",
        help="Перекодировать видео (лучше стыки между частями). Иначе — копия потока, качество как в источнике.",
    )
    ap.add_argument(
        "--crf",
        type=int,
        default=18,
        help="CRF libx264 при --reencode-video (меньше — выше качество, больше файл)",
    )
    ap.add_argument(
        "--preset",
        default="slow",
        help="Preset libx264 при --reencode-video (по умолчанию slow — лучше качество/размер)",
    )
    ap.add_argument(
        "--fast-seek",
        action="store_true",
        help="Быстрый -ss до входа (быстрее, границы менее точные). По умолчанию -ss после -i.",
    )
    ap.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Не удалять временный файл после replace/none",
    )

    args = ap.parse_args()
    env_chunk = os.environ.get("VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_SECONDS", "").strip()
    if env_chunk:
        try:
            args.demucs_chunk_seconds = float(env_chunk.replace(",", "."))
        except ValueError:
            sys.stderr.write(
                f"Предупреждение: не число в VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_SECONDS={env_chunk!r}, оставляю CLI.\n"
            )
    if args.demucs_chunk_seconds < 0:
        ap.error("--demucs-chunk-seconds / env: не может быть отрицательным")
    env_olap = os.environ.get("VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_OVERLAP", "").strip()
    if env_olap:
        try:
            args.demucs_chunk_overlap = float(env_olap.replace(",", "."))
        except ValueError:
            sys.stderr.write(
                f"Предупреждение: не число в VIDEO_AUDIO_SPLIT_DEMUCS_CHUNK_OVERLAP={env_olap!r}, оставляю CLI.\n"
            )
    if args.demucs_chunk_overlap < 0:
        ap.error("--demucs-chunk-overlap / env: не может быть отрицательным")
    if (
        args.demucs_chunk_seconds > 0
        and args.demucs_chunk_overlap > 0
        and args.demucs_chunk_overlap >= args.demucs_chunk_seconds
    ):
        ap.error("--demucs-chunk-overlap должно быть строго меньше --demucs-chunk-seconds")
    if args.demucs_python is None:
        envp = os.environ.get("VIDEO_AUDIO_SPLIT_DEMUCS_PYTHON") or os.environ.get("DEMUCS_PYTHON")
        if envp:
            args.demucs_python = Path(envp.strip().strip('"').strip("'"))
    if args.demucs_python is not None:
        dp = args.demucs_python.expanduser().resolve()
        if not dp.is_file():
            ap.error(f"--demucs-python / env: не найден файл: {dp}")
        args.demucs_python = dp
    args.music_mix_volume = max(0.01, min(0.6, float(args.music_mix_volume)))
    if args.audio == "vocals_music":
        if args.music is None or not args.music.expanduser().resolve().is_file():
            ap.error("--audio vocals_music: укажите существующий --music")
    if args.audio in ("vocals", "vocals_music"):
        if importlib.util.find_spec("demucs") is None:
            ap.error("Режим vocals*: нужен пакет demucs (pip install demucs) и рабочий PyTorch.")
    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    max_seg = parse_max_duration(args.max_duration)
    seg_path = (
        args.segments.expanduser().resolve()
        if args.segments is not None
        else (default_json if not args.auto else None)
    )

    rows: list[SegmentRow]

    smart_window = parse_max_duration(args.smart_cut_window)
    min_seg = parse_max_duration(args.min_segment)
    if args.auto and min_seg >= max_seg:
        ap.error("--min-segment должно быть меньше --max-duration")

    if args.metadata_only and args.auto:
        if not args.input:
            ap.error("С --metadata-only --auto укажите видео (для измерения длительности)")
        ffmpeg = which_or_die("ffmpeg")
        ffprobe = which_or_die("ffprobe")
        video = args.input.expanduser().resolve()
        if not video.is_file():
            sys.stderr.write(f"Файл не найден: {video}\n")
            sys.exit(1)
        rows = build_auto_plan_from_video(
            ffmpeg,
            ffprobe,
            video,
            max_seg,
            args.part_prefix,
            smart_cut=not args.no_smart_cut,
            window_sec=smart_window,
            min_seg_sec=min_seg,
            scene_threshold=args.scene_threshold,
            silence_noise_db=args.silence_noise_db,
            silence_min_sec=args.silence_min,
        )
        if args.youtube_placeholders:
            rows = apply_youtube_placeholders(rows, video.stem)
        write_all_youtube_sidecars(out_dir, rows, video.suffix or ".mp4")
        if args.write_segments_json:
            args.write_segments_json.parent.mkdir(parents=True, exist_ok=True)
            args.write_segments_json.write_text(
                json.dumps(rows_to_json_list(rows), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            sys.stderr.write(f"Разметка: {args.write_segments_json}\n")
        sys.stderr.write("Метаданные (авто) записаны.\n")
        return

    if args.metadata_only:
        if not seg_path or not seg_path.is_file():
            ap.error("Укажите --segments FILE.json или используйте --metadata-only --auto с видео")
        if args.input:
            sys.stderr.write("Режим --metadata-only: входное видео игнорируется.\n")
        rows = load_segment_rows(seg_path)
        write_all_youtube_sidecars(out_dir, rows, ".mp4")
        sys.stderr.write("Метаданные для YouTube записаны (без нарезки видео).\n")
        return

    if not args.input:
        ap.error("Укажите входной видеофайл")

    ffmpeg = which_or_die("ffmpeg")
    ffprobe = which_or_die("ffprobe")

    video = args.input.expanduser().resolve()
    if not video.is_file():
        sys.stderr.write(f"Файл не найден: {video}\n")
        sys.exit(1)

    vid_len = ffprobe_duration(ffprobe, video)

    if args.auto:
        rows = build_auto_plan_from_video(
            ffmpeg,
            ffprobe,
            video,
            max_seg,
            args.part_prefix,
            smart_cut=not args.no_smart_cut,
            window_sec=smart_window,
            min_seg_sec=min_seg,
            scene_threshold=args.scene_threshold,
            silence_noise_db=args.silence_noise_db,
            silence_min_sec=args.silence_min,
        )
        if args.youtube_placeholders:
            rows = apply_youtube_placeholders(rows, video.stem)
        sys.stderr.write(
            f"Режим --auto: длительность {vid_len:.1f}s, "
            f"макс. кусок {max_seg:.1f}s, частей: {len(rows)}\n"
        )
    else:
        path = seg_path if seg_path and seg_path.is_file() else default_json
        if not path.is_file():
            sys.stderr.write(f"JSON не найден: {path}\n")
            sys.exit(1)
        rows = load_segment_rows(path)

    if args.write_segments_json:
        args.write_segments_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_segments_json.write_text(
            json.dumps(rows_to_json_list(rows), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        sys.stderr.write(f"Разметка сохранена: {args.write_segments_json}\n")

    last_end = max(s.end for s in rows)
    if last_end > vid_len + 0.5:
        sys.stderr.write(
            f"Предупреждение: разметка до {last_end:.1f}s, длительность файла {vid_len:.1f}s.\n"
        )

    if not args.reencode_video:
        sys.stderr.write(
            "Видео: потоковое копирование (без перекодирования) — качество как в исходнике. "
            "При артефактах на стыках используйте --reencode-video.\n"
        )

    work_src = video
    tmp_path: Path | None = None

    if args.audio != "copy":
        suffix = video.suffix or ".mp4"
        tmp_path = out_dir / f"_{video.stem}_work_audio{suffix}"
        sys.stderr.write(f"Подготовка аудио ({args.audio}) -> {tmp_path.name}\n")
        build_audio_filtered_video(
            ffmpeg,
            ffprobe,
            video,
            tmp_path,
            args.audio,
            args.music,
            args.audio_bitrate,
            demucs_model=args.demucs_model,
            demucs_device=args.demucs_device,
            demucs_python=args.demucs_python,
            music_mix_volume=args.music_mix_volume,
            demucs_chunk_seconds=args.demucs_chunk_seconds,
            demucs_chunk_overlap_sec=args.demucs_chunk_overlap,
        )
        work_src = tmp_path

    try:
        for seg in rows:
            out_name = f"{seg.name}{video.suffix or '.mp4'}"
            dest = out_dir / out_name
            sys.stderr.write(
                f"Нарезка {seg.name}: {fmt_ffmpeg_time(seg.start)} + {fmt_ffmpeg_time(seg.duration)} -> {dest.name}\n"
            )
            split_segment(
                ffmpeg,
                work_src,
                seg,
                dest,
                reencode_video=args.reencode_video,
                video_crf=args.crf,
                video_preset=args.preset,
                fast_seek=args.fast_seek,
            )
        write_all_youtube_sidecars(out_dir, rows, video.suffix or ".mp4")
    finally:
        if tmp_path and tmp_path.is_file() and not args.keep_intermediate:
            tmp_path.unlink(missing_ok=True)

    sys.stderr.write("Готово.\n")


if __name__ == "__main__":
    main()
