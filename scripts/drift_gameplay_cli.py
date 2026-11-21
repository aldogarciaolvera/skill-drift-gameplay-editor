# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#     "faster-whisper>=1.0",
#     "numpy>=1.26,<2.1",
#     "setuptools>=65,<81",
#     "tensorflow>=2.15,<2.19",
#     "tensorflow-hub>=0.16",
#     "tf-keras>=2.15,<2.19",
# ]
# ///
"""CLI companion for CutWire Drift gameplay editing workflows.

Provides deterministic local media analysis (ffmpeg pause/screen scanning,
YAMNet music/speech detection, faster-whisper word-level transcription),
YouTube 16:9 apply-batch & metadata planning, Reels 9:16 highlight scoring,
dual EN/ES SRT generation, and UTF-8 JSON-RPC communication with Drift /mcp.
"""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# Shared IO, Binary, Time & Interval Utilities
# ---------------------------------------------------------------------------


def read_text_auto(path: str | Path) -> str:
    """Read UTF-8 (with/without BOM) or UTF-16 text files safely on Windows."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def read_json_auto(path: str | Path) -> Any:
    """Parse JSON from UTF-8/UTF-16 files."""
    return json.loads(read_text_auto(path))


def write_json(path: str | Path, data: Any) -> Path:
    """Write formatted UTF-8 JSON, creating parent directories if needed."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def write_text(path: str | Path, text: str) -> Path:
    """Write UTF-8 text file, creating parent directories if needed."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def require_binary(name: str) -> str:
    """Locate an external binary on PATH or raise a clear actionable error."""
    found = shutil.which(name)
    if not found:
        raise RuntimeError(
            f"Required binary '{name}' not found on PATH. Install {name} and ensure it is available on PATH."
        )
    return found


def retry_with_backoff(
    fn: Callable[[], Any],
    attempts: int = 3,
    base_delay: float = 2.0,
    label: str = "operation",
) -> Any:
    """Retry transient failures (e.g., initial TFHub/HuggingFace downloads)."""
    last_exc: Exception | None = None
    for idx in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if idx + 1 < attempts:
                delay = base_delay * (2**idx)
                print(
                    f"[warn] {label} failed ({exc}); retrying in {delay:.1f}s...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_exc}") from last_exc


def smooth_median(values: Sequence[float], radius: int) -> list[float]:
    """Apply a centered sliding-window median filter."""
    n = len(values)
    if n == 0 or radius <= 0:
        return list(values)
    out: list[float] = []
    for i in range(n):
        window = sorted(values[max(0, i - radius) : min(n, i + radius + 1)])
        out.append(float(window[len(window) // 2]))
    return out


def flags_to_intervals(
    flags: Sequence[bool],
    times: Sequence[float],
    hop: float,
) -> list[tuple[float, float]]:
    """Convert boolean frame flags into [start, end] time spans."""
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = float(times[i])
        elif not flag and start is not None:
            intervals.append((start, float(times[i])))
            start = None
    if start is not None and times:
        intervals.append((start, float(times[-1]) + hop))
    return intervals


def merge_intervals(
    intervals: Sequence[tuple[float, float]],
    max_gap: float = 0.0,
) -> list[tuple[float, float]]:
    """Sort and merge overlapping or nearby [start, end] intervals."""
    if not intervals:
        return []
    ordered = sorted((float(a), float(b)) for a, b in intervals if b > a)
    merged: list[list[float]] = []
    for a, b in ordered:
        if merged and a - merged[-1][1] <= max_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def subtract_intervals(
    base: Sequence[tuple[float, float]],
    cutouts: Sequence[tuple[float, float]],
    min_len: float = 0.0,
) -> list[tuple[float, float]]:
    """Subtract cutout intervals from base intervals, keeping spans >= min_len."""
    if not base:
        return []
    merged_cuts = merge_intervals(cutouts, max_gap=0.0)
    result: list[tuple[float, float]] = []
    for a, b in base:
        pieces: list[tuple[float, float]] = [(float(a), float(b))]
        for ca, cb in merged_cuts:
            nxt: list[tuple[float, float]] = []
            for px, py in pieces:
                if cb <= px or ca >= py:
                    nxt.append((px, py))
                    continue
                if ca > px:
                    nxt.append((px, ca))
                if cb < py:
                    nxt.append((cb, py))
            pieces = nxt
        for px, py in pieces:
            if py - px >= min_len:
                result.append((round(px, 3), round(py, 3)))
    return result


def format_srt_timestamp(seconds: float) -> str:
    """Format non-negative seconds as HH:MM:SS,mmm for SubRip (.srt)."""
    total_ms = max(0, int(round(seconds * 1000.0)))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    minutes = total_min % 60
    hours = total_min // 60
    return f"{hours:02d}:{minutes:02d}:{sec:02d},{ms:03d}"


def format_youtube_timestamp(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS for YouTube chapters."""
    total_sec = max(0, int(round(seconds)))
    hours, rem = divmod(total_sec, 3600)
    minutes, sec = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def norm_path_key(path: str) -> str:
    """Normalize filesystem paths for cross-OS asset matching."""
    return os.path.normcase(os.path.normpath(path.replace("\\", "/")))


def probe_video(video_path: str | Path) -> dict[str, Any]:
    """Inspect video metadata using ffprobe."""
    ffprobe = require_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for '{video_path}': {proc.stderr.strip()}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not vstream:
        raise RuntimeError(f"No video stream found in '{video_path}'.")
    width = int(vstream.get("width", 0))
    height = int(vstream.get("height", 0))
    fmt = info.get("format", {})
    dur = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    fps = 30.0
    rate = str(vstream.get("r_frame_rate") or "30/1")
    if "/" in rate:
        num, den = rate.split("/", 1)
        if float(den) > 0:
            fps = float(num) / float(den)
    elif rate:
        fps = float(rate)
    return {
        "path": str(Path(video_path).resolve()),
        "width": width,
        "height": height,
        "duration": round(dur, 3),
        "fps": round(fps, 3),
        "has_audio": astream is not None,
    }


def extract_f32_audio(src_path: str | Path, out_f32: str | Path, sample_rate: int = 16000) -> Path:
    """Extract mono float32 PCM audio via ffmpeg."""
    ffmpeg = require_binary("ffmpeg")
    out = Path(out_f32)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(src_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg audio extraction failed for '{src_path}': {proc.stderr.strip()}")
    return out


# ---------------------------------------------------------------------------
# Subcommand: mcp
# ---------------------------------------------------------------------------


def _parse_mcp_body(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in content_type.lower() or text.startswith("data:"):
        last_obj: dict[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    last_obj = json.loads(payload)
        if last_obj is None:
            raise ValueError(f"Empty SSE response from Drift MCP: {text[:300]}")
        return last_obj
    return json.loads(text)


def _post_mcp_tool(
    url: str,
    token: str,
    tool: str,
    tool_args: Any,
    timeout: int = 1800,
    img_dir: Path | None = None,
) -> tuple[Any, list[str], bool]:
    """Execute one Drift /mcp JSON-RPC tool call and decode text + image blocks."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": tool_args},
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"http_{exc.code}", "detail": detail}, [], False
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        return {"ok": False, "error": "connection_failed", "detail": str(exc)}, [], False

    rpc = _parse_mcp_body(raw, ctype)
    if "error" in rpc and rpc["error"] is not None:
        return {"ok": False, "error": "jsonrpc_error", "rpc_error": rpc["error"]}, [], False

    res_obj = rpc.get("result", {})
    sc = res_obj.get("structuredContent")
    content = res_obj.get("content", [])
    text_blocks: list[Any] = []
    saved_images: list[str] = []

    for idx, block in enumerate(content):
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            try:
                text_blocks.append(json.loads(txt))
            except json.JSONDecodeError:
                text_blocks.append(txt)
        elif btype == "image" and img_dir is not None:
            mime = block.get("mimeType", "image/jpeg")
            ext = "png" if "png" in mime.lower() else "jpg"
            img_dir.mkdir(parents=True, exist_ok=True)
            img_path = img_dir / f"{tool}_{idx}.{ext}"
            img_path.write_bytes(base64.b64decode(block.get("data", "")))
            saved_images.append(str(img_path))

    if sc is not None and not saved_images:
        final_obj: Any = sc
    elif len(text_blocks) == 1 and isinstance(text_blocks[0], dict) and not saved_images:
        final_obj = text_blocks[0]
    elif len(text_blocks) == 1 and isinstance(text_blocks[0], dict) and saved_images:
        final_obj = dict(text_blocks[0])
        final_obj["saved_images"] = saved_images
    else:
        final_obj = {
            "ok": True,
            "tool": tool,
            "text_blocks": text_blocks,
            "saved_images": saved_images,
        }
    ok_flag = final_obj.get("ok", True) if isinstance(final_obj, dict) else True
    return final_obj, saved_images, (ok_flag is not False)


def _resolve_mcp_credentials(args: argparse.Namespace) -> tuple[str, str]:
    url = getattr(args, "url", None) or os.environ.get("DRIFT_MCP_URL", "")
    token = getattr(args, "token", None) or os.environ.get("DRIFT_MCP_TOKEN", "")
    return str(url).strip(), str(token).strip()


def cmd_mcp(args: argparse.Namespace) -> int:
    """Invoke a CutWire Drift /mcp JSON-RPC tool and save response to --output."""
    url, token = _resolve_mcp_credentials(args)
    if not url or not token:
        err = {
            "ok": False,
            "error": "missing_credentials",
            "detail": "Both --url (or DRIFT_MCP_URL) and --token (or DRIFT_MCP_TOKEN) are required.",
        }
        write_json(args.output, err)
        print(f"[mcp] ERROR missing credentials -> {args.output}", file=sys.stderr)
        return 1

    if args.args_file:
        tool_args = read_json_auto(args.args_file)
    else:
        tool_args = json.loads(args.args_json or "{}")

    img_dir = (
        Path(args.save_images_dir)
        if args.save_images_dir
        else Path(args.output).parent / f"{Path(args.output).stem}_images"
    )
    final_obj, saved_images, is_ok = _post_mcp_tool(
        url, token, args.tool, tool_args, timeout=args.timeout, img_dir=img_dir
    )
    write_json(args.output, final_obj)
    err_kind = final_obj.get("error") if isinstance(final_obj, dict) else None
    if not is_ok and err_kind in ("connection_failed", "jsonrpc_error") or (isinstance(err_kind, str) and err_kind.startswith("http_")):
        print(f"[mcp] ERROR {err_kind} -> {args.output}", file=sys.stderr)
        return 1
    ok_flag = final_obj.get("ok", True) if isinstance(final_obj, dict) else True
    print(f"[mcp] tool={args.tool} ok={ok_flag} images={len(saved_images)} -> {args.output}")
    return 0 if ok_flag is not False else 1


# ---------------------------------------------------------------------------
# Subcommand: scan-pauses
# ---------------------------------------------------------------------------


def _parse_crop_spec(spec: str | None, width: int, height: int) -> str:
    """Convert pixel w:h:x:y or fractional 0..1 fw:fh:fx:fy crop into ffmpeg crop=."""
    if not spec:
        return f"crop={width}:{height}:0:0"
    parts = [float(p) for p in spec.split(":")]
    if len(parts) != 4:
        raise ValueError(f"Invalid crop spec '{spec}', expected w:h:x:y")
    if all(0.0 <= p <= 1.0 for p in parts):
        cw = max(16, int(round(parts[0] * width)))
        ch = max(16, int(round(parts[1] * height)))
        cx = max(0, int(round(parts[2] * width)))
        cy = max(0, int(round(parts[3] * height)))
    else:
        cw, ch, cx, cy = (int(p) for p in parts)
    cw = min(cw, max(1, width - cx))
    ch = min(ch, max(1, height - cy))
    return f"crop={cw}:{ch}:{cx}:{cy}"


def _build_scan_filter(width: int, height: int, crop_spec: str | None = None) -> tuple[str, list[tuple[str, int, int]]]:
    """Build grayscale vstack filter combining full-frame, left panel, and top bar."""
    if crop_spec:
        crop = _parse_crop_spec(crop_spec, width, height)
        vf = f"{crop},scale=64:36:flags=area,format=gray"
        return vf, [("custom", 0, 64 * 36)]

    left_crop = _parse_crop_spec("0.32:0.75:0.0:0.0", width, height)
    top_crop = _parse_crop_spec("1.0:0.08:0.0:0.0", width, height)
    vf = (
        f"split=3[f][l][t];"
        f"[f]scale=64:36:flags=area[fa];"
        f"[l]{left_crop},scale=64:36:flags=area[la];"
        f"[t]{top_crop},scale=64:6:flags=area[ta];"
        f"[fa][la][ta]vstack=inputs=3,format=gray"
    )
    slices = [
        ("full", 0, 64 * 36),
        ("left", 64 * 36, 2 * 64 * 36),
        ("top", 2 * 64 * 36, 2 * 64 * 36 + 64 * 6),
    ]
    return vf, slices


def _read_ref_bytes(png_path: str | Path, vf: str, nbytes: int) -> bytes:
    ffmpeg = require_binary("ffmpeg")
    cmd = [ffmpeg, "-v", "error", "-i", str(png_path), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or len(proc.stdout) < nbytes:
        raise RuntimeError(f"Failed to read reference image '{png_path}': {proc.stderr.decode(errors='ignore')}")
    return proc.stdout[:nbytes]


def _bytes_mae(a: bytes, b: bytes, start: int = 0, end: int | None = None) -> float:
    end_idx = len(a) if end is None else end
    length = end_idx - start
    if length <= 0:
        return 255.0
    return sum(abs(a[i] - b[i]) for i in range(start, end_idx)) / float(length)


def _match_reference(frame: bytes, ref: bytes, slices: list[tuple[str, int, int]]) -> float:
    """Compute best region-aware MAE against a reference frame."""
    if len(slices) == 1:
        _, s, e = slices[0]
        return _bytes_mae(frame, ref, s, e)
    scores = [_bytes_mae(frame, ref, s, e) for _, s, e in slices]
    return min(scores[0], 0.5 * scores[0] + 0.5 * min(scores[1:]))


def _refine_windows_ffmpeg(
    video_path: str | Path,
    windows: list[tuple[float, float]],
    vf: str,
    nbytes: int,
    ref_bytes: bytes,
    slices: list[tuple[str, int, int]],
    mae_thresh: float,
    refine_fps: float,
) -> list[dict[str, Any]]:
    """Run high-FPS ffmpeg edge scans over candidate windows."""
    ffmpeg = require_binary("ffmpeg")
    refined: list[dict[str, Any]] = []
    dt = 1.0 / refine_fps
    for w_start, w_end in windows:
        dur = max(0.5, w_end - w_start)
        cmd = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{w_start:.3f}",
            "-t",
            f"{dur:.3f}",
            "-i",
            str(video_path),
            "-an",
            "-vf",
            f"fps={refine_fps:g},{vf}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            assert proc.stdout is not None
            idx = 0
            active_start: float | None = None
            maes: list[float] = []
            while True:
                buf = proc.stdout.read(nbytes)
                if len(buf) < nbytes:
                    break
                t = w_start + idx * dt
                mae = _match_reference(buf, ref_bytes, slices)
                if mae < mae_thresh:
                    maes.append(mae)
                    if active_start is None:
                        active_start = t
                elif active_start is not None:
                    refined.append(
                        {
                            "start": round(active_start, 3),
                            "end": round(t, 3),
                            "duration": round(t - active_start, 3),
                            "mean_mae": round(sum(maes) / max(1, len(maes)), 2),
                        }
                    )
                    active_start = None
                    maes = []
                idx += 1
            proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            proc.wait()
            if active_start is not None:
                t_end = w_start + idx * dt
                refined.append(
                    {
                        "start": round(active_start, 3),
                        "end": round(t_end, 3),
                        "duration": round(t_end - active_start, 3),
                        "mean_mae": round(sum(maes) / max(1, len(maes)), 2),
                    }
                )
    return refined


def cmd_scan_pauses(args: argparse.Namespace) -> int:
    """Two-phase ffmpeg scan for pause menus, maps, static loading/dead screens, and motion."""
    ffmpeg = require_binary("ffmpeg")
    meta = probe_video(args.video)
    w, h, dur = meta["width"], meta["height"], meta["duration"]

    vf_pause, pause_slices = _build_scan_filter(w, h, args.crop_pause)
    n_pause = pause_slices[-1][2]
    vf_map, map_slices = _build_scan_filter(w, h, args.crop_map)
    n_map = map_slices[-1][2]

    pause_refs = [_read_ref_bytes(p, vf_pause, n_pause) for p in (args.ref_pause or [])]
    map_refs = [_read_ref_bytes(p, vf_map, n_map) for p in (args.ref_map or [])]

    # Phase 1: keyframe pass with base filter (full + left + top) for static + ref checks
    base_vf, base_slices = _build_scan_filter(w, h, None)
    n_base = base_slices[-1][2]
    cmd = [
        ffmpeg,
        "-v",
        "info",
        "-skip_frame",
        "nokey",
        "-an",
        "-i",
        str(args.video),
        "-vf",
        f"{base_vf},showinfo",
        "-vsync",
        "vfr",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        assert proc.stdout is not None and proc.stderr is not None

        pts_list: list[float] = []

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for raw_line in iter(proc.stderr.readline, b""):
                m = re.search(rb"pts_time:([0-9.]+)", raw_line)
                if m:
                    pts_list.append(float(m.group(1)))

        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_err.start()

        kf_frames: list[bytes] = []
        while True:
            chunk = proc.stdout.read(n_base)
            if len(chunk) < n_base:
                break
            kf_frames.append(chunk)
        proc.stdout.close()
        t_err.join()
        proc.stderr.close()
        proc.wait()

    if len(pts_list) < len(kf_frames):
        step = dur / max(1, len(kf_frames))
        pts_list = [i * step for i in range(len(kf_frames))]

    # Detect static keyframe runs and moderate/high motion runs
    static_spans: list[tuple[float, float, float]] = []
    motion_spans: list[tuple[float, float]] = []
    static_start: float | None = None
    static_diffs: list[float] = []
    motion_start: float | None = None

    pause_cand_wins: list[tuple[float, float]] = []
    map_cand_wins: list[tuple[float, float]] = []

    for i, frame in enumerate(kf_frames):
        t = pts_list[i]
        next_t = pts_list[i + 1] if i + 1 < len(pts_list) else dur
        if i > 0:
            d_full = _bytes_mae(frame, kf_frames[i - 1], 0, 64 * 36)
            if d_full < args.static_diff_thresh:
                static_diffs.append(d_full)
                if static_start is None:
                    static_start = pts_list[i - 1]
            else:
                if static_start is not None and t - static_start >= args.min_static_sec:
                    static_spans.append((static_start, t, sum(static_diffs) / max(1, len(static_diffs))))
                static_start = None
                static_diffs = []

            if d_full >= 4.0:
                if motion_start is None:
                    motion_start = pts_list[i - 1]
            elif motion_start is not None:
                if t - motion_start >= 10.0:
                    motion_spans.append((motion_start, t))
                motion_start = None

        if pause_refs and not args.crop_pause:
            if any(_match_reference(frame, r, base_slices) < args.mae_thresh + 4.0 for r in pause_refs):
                pause_cand_wins.append((max(0.0, t - 3.0), min(dur, next_t + 3.0)))
        if map_refs and not args.crop_map:
            if any(_match_reference(frame, r, base_slices) < args.mae_thresh + 4.0 for r in map_refs):
                map_cand_wins.append((max(0.0, t - 3.0), min(dur, next_t + 3.0)))

    if static_start is not None and dur - static_start >= args.min_static_sec:
        static_spans.append((static_start, dur, sum(static_diffs) / max(1, len(static_diffs))))
    if motion_start is not None and dur - motion_start >= 10.0:
        motion_spans.append((motion_start, dur))

    # If custom crops were provided or refs are given, also probe head/tail + static runs
    if pause_refs:
        if args.crop_pause:
            pause_cand_wins = [(0.0, dur)] if dur <= 120.0 else [
                (0.0, min(dur, 45.0)),
                *((max(0.0, a - 2.0), min(dur, b + 2.0)) for a, b, _ in static_spans),
                (max(0.0, dur - 45.0), dur),
            ]
        else:
            pause_cand_wins.extend([(0.0, min(dur, 40.0)), (max(0.0, dur - 40.0), dur)])
            pause_cand_wins.extend((max(0.0, a - 2.0), min(dur, b + 2.0)) for a, b, _ in static_spans)
        pause_cand_wins = merge_intervals(pause_cand_wins, max_gap=2.0)

    if map_refs:
        if args.crop_map:
            map_cand_wins = [(0.0, dur)] if dur <= 120.0 else [
                (max(0.0, a - 2.0), min(dur, b + 2.0)) for a, b, _ in static_spans
            ]
        map_cand_wins = merge_intervals(map_cand_wins, max_gap=2.0)

    # Phase 2: exact edge refinement
    pause_segments: list[dict[str, Any]] = []
    for ref in pause_refs:
        hits = _refine_windows_ffmpeg(
            args.video,
            pause_cand_wins,
            vf_pause,
            n_pause,
            ref,
            pause_slices,
            args.mae_thresh,
            args.refine_fps,
        )
        for item in hits:
            if item["duration"] >= args.min_pause_sec:
                item["kind"] = "pause"
                item["confidence"] = "high"
                pause_segments.append(item)

    map_segments: list[dict[str, Any]] = []
    review_candidates: list[dict[str, Any]] = []
    for ref in map_refs:
        hits = _refine_windows_ffmpeg(
            args.video,
            map_cand_wins,
            vf_map,
            n_map,
            ref,
            map_slices,
            args.mae_thresh,
            args.refine_fps,
        )
        for item in hits:
            if item["duration"] >= args.min_map_sec:
                item["kind"] = "map"
                item["confidence"] = "high"
                map_segments.append(item)
            elif item["duration"] >= 1.5:
                item["kind"] = "short_map"
                item["confidence"] = "ambiguous"
                item["reason"] = f"Short map screen ({item['duration']:.2f}s < {args.min_map_sec:.1f}s); verify before cutting."
                review_candidates.append(item)

    static_segments = [
        {
            "start": round(a, 3),
            "end": round(b, 3),
            "duration": round(b - a, 3),
            "mean_diff": round(md, 2),
            "kind": "static_screen",
            "confidence": "medium",
        }
        for a, b, md in static_spans
    ]

    # Build merged suggested_cuts (pause + long map + uncovered static loading screens)
    confirmed_tuples = [(p["start"], p["end"]) for p in pause_segments] + [
        (m["start"], m["end"]) for m in map_segments
    ]
    suggested_cuts: list[dict[str, Any]] = []
    for seg in pause_segments + map_segments:
        suggested_cuts.append(
            {
                "start": seg["start"],
                "end": seg["end"],
                "duration": seg["duration"],
                "kind": seg["kind"],
                "confidence": seg["confidence"],
                "transition": True,
            }
        )
    for st in static_segments:
        uncovered = subtract_intervals([(st["start"], st["end"])], confirmed_tuples, min_len=args.min_static_sec)
        for ua, ub in uncovered:
            review_candidates.append(
                {
                    "start": ua,
                    "end": ub,
                    "duration": round(ub - ua, 3),
                    "kind": "static_screen",
                    "confidence": "ambiguous",
                    "reason": "Static/frozen frames without pause/map reference match (loading screen or idle). Confirm visually.",
                }
            )
    suggested_cuts.sort(key=lambda x: x["start"])

    out_payload = {
        "ok": True,
        "video": meta,
        "keyframes_scanned": len(kf_frames),
        "pause_segments": sorted(pause_segments, key=lambda x: x["start"]),
        "map_segments": sorted(map_segments, key=lambda x: x["start"]),
        "static_segments": static_segments,
        "suggested_cuts": suggested_cuts,
        "review_candidates": sorted(review_candidates, key=lambda x: x["start"]),
        "candidate_motion_segments": [
            {"start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3)}
            for a, b in merge_intervals(motion_spans, max_gap=4.0)
        ],
    }
    write_json(args.output, out_payload)
    print(
        f"[scan-pauses] kf={len(kf_frames)} cuts={len(suggested_cuts)} "
        f"review={len(review_candidates)} -> {args.output}"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: detect-music
# ---------------------------------------------------------------------------


SPEECH_CLASSES = (
    "Speech",
    "Male speech, man speaking",
    "Female speech, woman speaking",
    "Conversation",
    "Narration, monologue",
    "Child speech, kid speaking",
    "Shout",
    "Yell",
    "Whispering",
)
SINGING_CLASSES = ("Singing", "Rapping", "Song", "Male singing", "Female singing")
YAMNET_HOP_SEC = 0.48
YAMNET_SR = 16000


def classify_music_segments(
    times: Sequence[float],
    speech_scores: Sequence[float],
    music_scores: Sequence[float],
    sing_scores: Sequence[float],
    speech_thresh: float = 0.40,
    music_thresh: float = 0.30,
    sing_thresh: float = 0.15,
    min_music_sec: float = 4.0,
    min_mute_sec: float = 2.0,
    gap_merge_sec: float = 3.0,
    speech_pad_sec: float = 0.5,
) -> dict[str, Any]:
    """Classify YAMNet frame scores into music, speech, mute, and ambiguous review windows."""
    if not times:
        return {
            "music_windows": [],
            "speech_windows": [],
            "proposed_mute_windows": [],
            "review_windows": [],
            "stats": {"music_seconds": 0.0, "mute_seconds": 0.0, "review_seconds": 0.0},
        }

    mu_s = smooth_median(music_scores, radius=3)
    si_s = smooth_median(sing_scores, radius=3)
    sp_s = smooth_median(speech_scores, radius=2)

    dia_flags = [sp_s[i] >= speech_thresh and si_s[i] < sing_thresh for i in range(len(times))]
    music_flags = [mu_s[i] >= music_thresh for i in range(len(times))]

    raw_dia = merge_intervals(flags_to_intervals(dia_flags, times, YAMNET_HOP_SEC), max_gap=1.5)
    padded_dia = [(max(0.0, a - speech_pad_sec), b + speech_pad_sec) for a, b in raw_dia]
    padded_dia = merge_intervals(padded_dia, max_gap=0.0)

    raw_music = [
        (a, b)
        for a, b in merge_intervals(flags_to_intervals(music_flags, times, YAMNET_HOP_SEC), max_gap=gap_merge_sec)
        if b - a >= min_music_sec
    ]

    music_windows: list[dict[str, Any]] = []
    high_conf_music: list[tuple[float, float]] = []
    ambiguous_music: list[tuple[float, float]] = []

    for a, b in raw_music:
        idxs = [i for i, t in enumerate(times) if a <= t <= b]
        if not idxs:
            continue
        avg_m = sum(music_scores[i] for i in idxs) / len(idxs)
        max_m = max(music_scores[i] for i in idxs)
        avg_si = sum(sing_scores[i] for i in idxs) / len(idxs)
        max_si = max(sing_scores[i] for i in idxs)
        avg_sp = sum(speech_scores[i] for i in idxs) / len(idxs)
        high_conf = avg_m >= 0.42 or max_si >= sing_thresh
        conf = "high" if high_conf else "ambiguous"
        music_windows.append(
            {
                "start": round(a, 3),
                "end": round(b, 3),
                "duration": round(b - a, 3),
                "avg_music": round(avg_m, 3),
                "max_music": round(max_m, 3),
                "avg_singing": round(avg_si, 3),
                "max_singing": round(max_si, 3),
                "avg_speech": round(avg_sp, 3),
                "confidence": conf,
            }
        )
        if high_conf:
            high_conf_music.append((a, b))
        else:
            ambiguous_music.append((a, b))

    mute_tuples = subtract_intervals(high_conf_music, padded_dia, min_len=min_mute_sec)
    review_tuples = subtract_intervals(ambiguous_music, padded_dia, min_len=min_mute_sec)

    proposed_mute = [
        {"start": a, "end": b, "duration": round(b - a, 3)} for a, b in mute_tuples
    ]
    review_windows = [
        {
            "start": a,
            "end": b,
            "duration": round(b - a, 3),
            "reason": "Moderate instrumental score without clear vocals; verify whether licensed music or game score.",
        }
        for a, b in review_tuples
    ]
    speech_windows = [{"start": round(a, 3), "end": round(b, 3)} for a, b in padded_dia]

    return {
        "music_windows": music_windows,
        "speech_windows": speech_windows,
        "proposed_mute_windows": proposed_mute,
        "review_windows": review_windows,
        "stats": {
            "music_seconds": round(sum(w["duration"] for w in music_windows), 2),
            "mute_seconds": round(sum(w["duration"] for w in proposed_mute), 2),
            "review_seconds": round(sum(w["duration"] for w in review_windows), 2),
        },
    }


def cmd_detect_music(args: argparse.Namespace) -> int:
    """Extract 16kHz audio and run YAMNet classification to find music vs dialogue windows."""
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
    import numpy as np
    import tensorflow as tf
    import tensorflow_hub as hub

    src = args.video or args.audio
    if not src:
        raise ValueError("Specify either --video or --audio.")

    cleanup_f32 = False
    if Path(src).suffix.lower() == ".f32":
        f32_path = Path(src)
    elif args.keep_f32:
        f32_path = extract_f32_audio(src, args.keep_f32, sample_rate=YAMNET_SR)
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="drift_yamnet_", suffix=".f32", delete=False)
        tmp.close()
        cleanup_f32 = True
        f32_path = extract_f32_audio(src, tmp.name, sample_rate=YAMNET_SR)

    try:
        model = retry_with_backoff(
            lambda: hub.load("https://tfhub.dev/google/yamnet/1"),
            attempts=3,
            base_delay=2.0,
            label="YAMNet download/load",
        )
        with tf.io.gfile.GFile(model.class_map_path().numpy()) as f:
            names = [row["display_name"] for row in csv.DictReader(f)]
        speech_idx = [i for i, n in enumerate(names) if n in SPEECH_CLASSES]
        music_idx = names.index("Music")
        sing_idx = [i for i, n in enumerate(names) if n in SINGING_CLASSES]

        wav = np.fromfile(str(f32_path), dtype=np.float32)
        chunk_samples = max(YAMNET_SR, int(args.chunk_sec * YAMNET_SR))
        times: list[float] = []
        sp_vals: list[float] = []
        mu_vals: list[float] = []
        si_vals: list[float] = []

        for c0 in range(0, len(wav), chunk_samples):
            seg = wav[c0 : c0 + chunk_samples]
            if len(seg) < YAMNET_SR:
                break
            scores, _, _ = model(seg)
            arr = scores.numpy()
            for i in range(arr.shape[0]):
                times.append(round(c0 / YAMNET_SR + i * YAMNET_HOP_SEC, 3))
                sp_vals.append(float(arr[i, speech_idx].max()))
                mu_vals.append(float(arr[i, music_idx]))
                si_vals.append(float(arr[i, sing_idx].max()))

        classified = classify_music_segments(
            times=times,
            speech_scores=sp_vals,
            music_scores=mu_vals,
            sing_scores=si_vals,
            speech_thresh=args.speech_thresh,
            music_thresh=args.music_thresh,
            sing_thresh=args.sing_thresh,
            min_music_sec=args.min_music_sec,
            min_mute_sec=args.min_mute_sec,
            gap_merge_sec=args.gap_merge_sec,
            speech_pad_sec=args.speech_pad_sec,
        )
        payload = {
            "ok": True,
            "source_path": str(Path(src).resolve()),
            "audio_f32_path": str(f32_path.resolve()) if not cleanup_f32 else None,
            "duration_sec": round(len(wav) / YAMNET_SR, 3),
            **classified,
        }
        write_json(args.output, payload)
        print(
            f"[detect-music] music_wins={len(classified['music_windows'])} "
            f"mute_wins={len(classified['proposed_mute_windows'])} "
            f"review={len(classified['review_windows'])} -> {args.output}"
        )
        return 0
    finally:
        if cleanup_f32 and f32_path.exists():
            f32_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subcommand: transcribe
# ---------------------------------------------------------------------------


def _load_windows(windows_file: str | Path) -> list[dict[str, Any]]:
    data = read_json_auto(windows_file)
    if isinstance(data, dict):
        raw_list = data.get("music_windows") or data.get("windows") or data.get("proposed_mute_windows") or []
    elif isinstance(data, list):
        raw_list = data
    else:
        raise ValueError(f"Unsupported windows-file structure in '{windows_file}'.")

    windows: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_list, start=1):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            windows.append({"tag": f"W{idx}", "start": float(item[0]), "end": float(item[1])})
        elif isinstance(item, dict) and "start" in item and "end" in item:
            windows.append(
                {
                    "tag": str(item.get("tag") or f"W{idx}"),
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                }
            )
    return windows


def _prepare_cuda_dlls() -> None:
    """Register PyTorch / NVIDIA CUDA runtime DLL directories on Windows if installed."""
    if sys.platform != "win32" or getattr(_prepare_cuda_dlls, "_done", False):
        return
    _prepare_cuda_dlls._done = True  # type: ignore[attr-defined]
    import importlib.util

    for pkg in ("torch", "nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except Exception:  # noqa: BLE001
            spec = None
        for root in getattr(spec, "submodule_search_locations", None) or []:
            for sub in ("lib", "bin"):
                dll_dir = Path(root) / sub
                if dll_dir.is_dir():
                    try:
                        os.add_dll_directory(str(dll_dir))
                    except OSError:
                        pass
                    sdir = str(dll_dir)
                    if sdir not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = sdir + os.pathsep + os.environ.get("PATH", "")


def _load_whisper_model(model_name: str, device: str, compute_type: str):
    """Load faster-whisper with optional auto CUDA resolution and CPU fallback."""
    from faster_whisper import WhisperModel

    def _build(dev: str, ctype: str):
        if dev == "cuda":
            _prepare_cuda_dlls()
        resolved_ctype = ("float16" if dev == "cuda" else "int8") if ctype == "auto" else ctype
        return retry_with_backoff(
            lambda: WhisperModel(model_name, device=dev, compute_type=resolved_ctype),
            attempts=3,
            base_delay=2.0,
            label=f"Whisper model '{model_name}' ({dev}/{resolved_ctype})",
        )

    if device == "auto":
        try:
            return _build("cuda", compute_type), "cuda"
        except Exception as cuda_exc:  # noqa: BLE001
            print(f"[warn] CUDA Whisper unavailable ({cuda_exc}); falling back to CPU/int8.", file=sys.stderr, flush=True)
            return _build("cpu", "int8"), "cpu"
    return _build(device, compute_type), device


def cmd_transcribe(args: argparse.Namespace) -> int:
    """Run faster-whisper with word_timestamps over candidate windows or full audio."""
    import numpy as np

    src = Path(args.audio)
    if not src.exists():
        raise FileNotFoundError(f"Audio/video source not found: {src}")

    if not args.full and not args.windows_file:
        raise ValueError("Specify either --windows-file or --full.")

    # Always decode non-.f32 inputs via ffmpeg first to avoid PyAV metadata errors on long containers.
    cleanup_f32 = False
    if src.suffix.lower() == ".f32":
        f32_path = src
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="drift_whisper_", suffix=".f32", delete=False)
        tmp.close()
        cleanup_f32 = True
        f32_path = extract_f32_audio(src, tmp.name, sample_rate=YAMNET_SR)

    try:
        try:
            model, active_device = _load_whisper_model(args.model, args.device, args.compute_type)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load Whisper model '{args.model}': {exc}. "
                "Try switching --model to 'small' or 'base' with --device cpu."
            ) from exc

        wav = np.fromfile(str(f32_path), dtype=np.float32)
        total_dur = len(wav) / float(YAMNET_SR)

        if args.full:
            windows = [{"tag": "FULL", "start": 0.0, "end": total_dur}]
        else:
            windows = _load_windows(args.windows_file)

        out_segments: list[dict[str, Any]] = []
        detected_langs: dict[str, int] = {}

        for win in windows:
            s0 = max(0, int(win["start"] * YAMNET_SR))
            s1 = min(len(wav), int(win["end"] * YAMNET_SR))
            if s1 <= s0:
                continue
            seg_audio = wav[s0:s1]
            t_kw: dict[str, Any] = {
                "word_timestamps": True,
                "vad_filter": bool(args.vad_filter),
                "no_speech_threshold": float(args.no_speech_threshold),
                "condition_on_previous_text": False,
            }
            if args.language:
                t_kw["language"] = args.language
            try:
                segs, info = model.transcribe(seg_audio, **t_kw)
                segs_list = list(segs)
            except RuntimeError as rt_exc:
                if active_device != "cpu":
                    print(f"[warn] CUDA inference failed ({rt_exc}); retrying on CPU/int8.", file=sys.stderr, flush=True)
                    model, active_device = _load_whisper_model(args.model, "cpu", "int8")
                    segs, info = model.transcribe(seg_audio, **t_kw)
                    segs_list = list(segs)
                else:
                    raise
            lang = getattr(info, "language", None)
            if lang:
                detected_langs[lang] = detected_langs.get(lang, 0) + 1

            offset = win["start"]
            for seg in segs_list:
                words_out = []
                if seg.words:
                    for w in seg.words:
                        words_out.append(
                            {
                                "start": round(offset + float(w.start), 3),
                                "end": round(offset + float(w.end), 3),
                                "word": w.word.strip(),
                                "prob": round(float(getattr(w, "probability", 1.0)), 3),
                            }
                        )
                out_segments.append(
                    {
                        "tag": win["tag"],
                        "start": round(offset + float(seg.start), 3),
                        "end": round(offset + float(seg.end), 3),
                        "text": seg.text.strip(),
                        "no_speech_prob": round(float(seg.no_speech_prob), 3),
                        "avg_logprob": round(float(seg.avg_logprob), 3),
                        "words": words_out,
                    }
                )

        primary_lang = max(detected_langs, key=detected_langs.get) if detected_langs else args.language
        payload = {
            "ok": True,
            "source_path": str(src.resolve()),
            "duration_sec": round(total_dur, 3),
            "model": args.model,
            "device": active_device,
            "detected_language": primary_lang,
            "segments": out_segments,
        }
        write_json(args.output, payload)
        print(f"[transcribe] segments={len(out_segments)} lang={primary_lang} device={active_device} -> {args.output}")
        return 0
    finally:
        if cleanup_f32 and f32_path.exists():
            f32_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subcommand: plan-youtube
# ---------------------------------------------------------------------------


def _load_manifest_by_asset(path: str | Path | None) -> list[dict[str, Any]]:
    """Load single analysis JSON or list/dict manifest of per-asset JSONs."""
    if not path:
        return []
    data = read_json_auto(path)
    if isinstance(data, list):
        out: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, str):
                out.append(read_json_auto(item))
            elif isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        return data["items"]
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Unsupported manifest format in '{path}'.")


def _clip_matches_source(clip: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Match a Drift clip against a scan-pauses / detect-music / transcript JSON."""
    target_asset = payload.get("asset_id") or payload.get("assetId")
    if target_asset and clip.get("assetId") == target_asset:
        return True
    src = (
        payload.get("source_path")
        or (payload.get("video") or {}).get("path")
        or payload.get("path")
        or ""
    )
    if not src:
        return True
    clip_path = clip.get("path") or ""
    if clip_path and norm_path_key(clip_path) == norm_path_key(src):
        return True
    return Path(src).name.lower() == str(clip.get("name", "")).lower()


def _src_to_tl(clip: dict[str, Any], t_src: float) -> float:
    speed = float(clip.get("speed") or 1.0)
    return float(clip["start"]) + (float(t_src) - float(clip["inPoint"])) / speed


def _tl_to_src(clip: dict[str, Any], t_tl: float) -> float:
    speed = float(clip.get("speed") or 1.0)
    return float(clip["inPoint"]) + (float(t_tl) - float(clip["start"])) * speed


def _find_linked_audio(inspect_data: dict[str, Any], video_clip: dict[str, Any]) -> dict[str, Any] | None:
    link_id = video_clip.get("linkId")
    for tr in inspect_data.get("tracks", []):
        if tr.get("type") != "audio":
            continue
        for item in tr.get("items", []):
            if link_id and item.get("linkId") == link_id:
                return item
            if (
                item.get("assetId") == video_clip.get("assetId")
                and abs(float(item.get("start", -99)) - float(video_clip.get("start", 0))) < 0.1
            ):
                return item
    return None


def _build_volume_keyframes(
    intervals_tl: Sequence[tuple[float, float]],
    clip_start: float,
    clip_end: float,
    fade_sec: float,
) -> list[list[float]]:
    """Build strictly monotonic volume keyframes preserving [0, 1] ramps inside clip bounds."""
    margin = 0.01
    merged = merge_intervals(intervals_tl, max_gap=max(0.05, fade_sec * 2.0))
    clipped: list[tuple[float, float]] = []
    for a, b in merged:
        ca = max(clip_start + fade_sec + margin, a)
        cb = min(clip_end - fade_sec - margin, b)
        if cb - ca >= 0.25:
            clipped.append((round(ca, 3), round(cb, 3)))

    keys: list[list[float]] = [[round(clip_start, 3), 1.0]]
    for a, b in clipped:
        pts = [
            [round(a - fade_sec, 3), 1.0],
            [round(a, 3), 0.0],
            [round(b, 3), 0.0],
            [round(b + fade_sec, 3), 1.0],
        ]
        for t, v in pts:
            if t <= keys[-1][0]:
                t = round(keys[-1][0] + 0.005, 3)
            if t < clip_end:
                keys.append([t, v])
    if keys[-1][0] < round(clip_end, 3):
        keys.append([round(clip_end, 3), 1.0])
    return keys


def _pick_fill_track(
    fill_tracks: Sequence[dict[str, Any]],
    gap_start: float,
    gap_dur: float,
    hint_mood: str | None = None,
    idx_hint: int = 0,
) -> dict[str, Any]:
    if hint_mood:
        for tr in fill_tracks:
            if hint_mood.lower() in str(tr.get("mood", "")).lower():
                return tr
    return fill_tracks[idx_hint % len(fill_tracks)]


def cmd_plan_youtube(args: argparse.Namespace) -> int:
    """Plan phased Drift apply batches, YouTube credits, chapters, and edit report."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inspect_data = read_json_auto(args.inspect_file)
    pauses_list = _load_manifest_by_asset(args.pauses_file)
    music_list = _load_manifest_by_asset(args.music_file)
    transcript_list = _load_manifest_by_asset(args.transcript_file)
    fill_assets_raw = read_json_auto(args.fill_assets_file) if args.fill_assets_file else {}
    driving_raw = read_json_auto(args.driving_ranges_file) if args.driving_ranges_file else []

    video_tracks = [t for t in inspect_data.get("tracks", []) if t.get("type") == "video"]
    primary_vtrack = video_tracks[0] if video_tracks else {"items": [], "i": 0}
    vclips = sorted(primary_vtrack.get("items", []), key=lambda c: float(c.get("start", 0.0)))

    # ------------------------------------------------------------------
    # Stage: cuts (set_trim head/tail + descending split_clip)
    # ------------------------------------------------------------------
    if args.stage in ("cuts", "all"):
        ops_1a: list[dict[str, Any]] = []
        cuts_spec: list[dict[str, Any]] = []
        cumulative_shift = 0.0

        for clip in vclips:
            speed = float(clip.get("speed") or 1.0)
            in_pt = float(clip["inPoint"])
            out_pt = float(clip["outPoint"])
            effective_start = float(clip["start"]) - cumulative_shift

            c_cuts: list[dict[str, Any]] = []
            for p_doc in pauses_list:
                if _clip_matches_source(clip, p_doc):
                    c_cuts.extend(p_doc.get("suggested_cuts") or p_doc.get("cuts") or [])
            c_cuts = sorted(c_cuts, key=lambda x: float(x["start"]))

            new_in, new_out = in_pt, out_pt
            interior: list[dict[str, Any]] = []
            for cut in c_cuts:
                cs, ce = float(cut["start"]), float(cut["end"])
                if ce <= new_in or cs >= new_out:
                    continue
                if cs <= new_in + 0.5:
                    new_in = max(new_in, ce)
                elif ce >= new_out - 0.5:
                    new_out = min(new_out, cs)
                else:
                    interior.append(cut)

            if new_in > in_pt + 0.05 or new_out < out_pt - 0.05:
                ops_1a.append(
                    {
                        "tool": "set_trim",
                        "args": {
                            "clip": clip["id"],
                            "in": round(new_in, 3),
                            "out": round(new_out, 3),
                            "ripple": True,
                        },
                    }
                )
                old_span = (out_pt - in_pt) / speed
                new_span = (new_out - new_in) / speed
                cumulative_shift += max(0.0, old_span - new_span)

            split_times_tl: list[float] = []
            for cut in interior:
                cs = round(float(cut["start"]), 3)
                ce = round(float(cut["end"]), 3)
                if cs <= new_in + 0.05 or ce >= new_out - 0.05:
                    continue
                tl_s = effective_start + (cs - new_in) / speed
                tl_e = effective_start + (ce - new_in) / speed
                split_times_tl.extend([tl_s, tl_e])
                cuts_spec.append(
                    {
                        "assetId": clip.get("assetId"),
                        "source_start": cs,
                        "source_end": ce,
                        "expected_tl_start": round(tl_s, 3),
                        "kind": cut.get("kind", "pause"),
                        "transition": bool(cut.get("transition", True)),
                    }
                )

            for t_split in sorted(set(round(x, 3) for x in split_times_tl), reverse=True):
                ops_1a.append({"tool": "split_clip", "args": {"clip": clip["id"], "at": t_split}})

        write_json(out_dir / "batch_1a_trims_splits.json", {"ops": ops_1a})
        write_json(out_dir / "batch_1a_trims_splits_dry.json", {"ops": ops_1a, "dry_run": True})
        write_json(out_dir / "cuts_spec.json", {"cuts": cuts_spec})
        if args.stage == "cuts":
            print(f"[plan-youtube] stage=cuts ops={len(ops_1a)} interior_cuts={len(cuts_spec)} -> {out_dir}")
            return 0

    # ------------------------------------------------------------------
    # Stage: delete-cuts (match newly split pieces in apply-result geometry)
    # ------------------------------------------------------------------
    if args.stage == "delete-cuts":
        if not args.apply_result:
            raise ValueError("--apply-result is required when --stage=delete-cuts.")
        apply_res = read_json_auto(args.apply_result)
        cuts_spec_doc = read_json_auto(out_dir / "cuts_spec.json")
        cuts_spec = cuts_spec_doc.get("cuts", [])
        geom = apply_res.get("geometry", [])
        if not geom and "tracks" in apply_res:
            for tr in apply_res["tracks"]:
                if tr.get("type") == "video":
                    geom.extend(tr.get("items", []))

        delete_ids: list[str] = []
        for spec in cuts_spec:
            sa, sb = float(spec["source_start"]), float(spec["source_end"])
            for g in geom:
                g_in = float(g.get("in", g.get("inPoint", -1)))
                g_out = float(g.get("out", g.get("outPoint", -1)))
                if abs(g_in - sa) <= 0.25 and abs(g_out - sb) <= 0.25:
                    cid = g.get("id")
                    if cid and cid not in delete_ids:
                        delete_ids.append(cid)

        ops_1b = (
            [{"tool": "delete_clips", "args": {"clips": delete_ids, "ripple": True, "with_effects": True}}]
            if delete_ids
            else []
        )
        write_json(out_dir / "batch_1b_delete_cuts.json", {"ops": ops_1b})
        write_json(out_dir / "batch_1b_delete_cuts_dry.json", {"ops": ops_1b, "dry_run": True})
        print(f"[plan-youtube] stage=delete-cuts matched={len(delete_ids)} -> {out_dir}")
        return 0

    # ------------------------------------------------------------------
    # Stage: post-cuts / all (transitions, volume mutes, music fill, SFX, reports)
    # ------------------------------------------------------------------
    ops_2: list[dict[str, Any]] = []
    ops_3: list[dict[str, Any]] = []
    bookmarks_added: list[dict[str, Any]] = []
    time_jump_cuts: list[float] = []

    # 1. Transitions on video time jumps
    for i in range(len(vclips) - 1):
        cur_c = vclips[i]
        nxt_c = vclips[i + 1]
        cur_end = float(cur_c["start"]) + float(cur_c["duration"])
        abutting = abs(float(nxt_c["start"]) - cur_end) <= 0.08
        same_asset_jump = (
            cur_c.get("assetId") == nxt_c.get("assetId")
            and abs(float(nxt_c["inPoint"]) - float(cur_c["outPoint"])) > 0.5
        )
        diff_asset_seam = cur_c.get("assetId") != nxt_c.get("assetId")
        if abutting and (same_asset_jump or diff_asset_seam):
            ops_2.append(
                {
                    "tool": "add_transition",
                    "args": {
                        "clip": cur_c["id"],
                        "kind": args.transition_kind,
                        "duration": round(float(args.transition_dur), 3),
                        "linked_audio": True,
                    },
                }
            )
            time_jump_cuts.append(round(cur_end, 3))

    # 2. Dialogue protection from Whisper transcripts + Copyright mute keyframes
    muted_tl_intervals: list[tuple[float, float]] = []
    muted_summary: list[dict[str, Any]] = []

    for clip in vclips:
        a_clip = _find_linked_audio(inspect_data, clip)
        if not a_clip:
            continue

        # Collect speech windows in source space from Whisper if provided
        whisper_speech_src: list[tuple[float, float]] = []
        for t_doc in transcript_list:
            if _clip_matches_source(clip, t_doc):
                for seg in t_doc.get("segments", []):
                    if (
                        float(seg.get("no_speech_prob", 1.0)) < 0.55
                        and float(seg.get("avg_logprob", -2.0)) > -1.0
                        and len(seg.get("text", "").strip()) >= 3
                    ):
                        whisper_speech_src.append(
                            (max(0.0, float(seg["start"]) - 0.35), float(seg["end"]) + 0.35)
                        )

        mute_src_intervals: list[tuple[float, float]] = []
        for m_doc in music_list:
            if not _clip_matches_source(clip, m_doc):
                continue
            for mw in m_doc.get("proposed_mute_windows", []):
                mute_src_intervals.append((float(mw["start"]), float(mw["end"])))
            for rw in m_doc.get("review_windows", []):
                rs, re_ = float(rw["start"]), float(rw["end"])
                if float(clip["inPoint"]) <= rs <= float(clip["outPoint"]):
                    tl_r = round(_src_to_tl(clip, rs), 3)
                    bookmarks_added.append(
                        {"at": tl_r, "label": f"REVIEW_MUSIC ({rw['duration']:.1f}s)", "reason": rw.get("reason", "")}
                    )

        for p_doc in pauses_list:
            if not _clip_matches_source(clip, p_doc):
                continue
            for rc in p_doc.get("review_candidates", []):
                rs = float(rc["start"])
                if float(clip["inPoint"]) <= rs <= float(clip["outPoint"]):
                    tl_r = round(_src_to_tl(clip, rs), 3)
                    bookmarks_added.append(
                        {"at": tl_r, "label": f"REVIEW_{str(rc.get('kind', 'CUT')).upper()}", "reason": rc.get("reason", "")}
                    )

        # Refine mute intervals against Whisper dialogue guard
        if whisper_speech_src and mute_src_intervals:
            mute_src_intervals = subtract_intervals(mute_src_intervals, whisper_speech_src, min_len=1.5)

        # Map onto clip timeline bounds
        clip_mutes_tl: list[tuple[float, float]] = []
        in_pt, out_pt = float(clip["inPoint"]), float(clip["outPoint"])
        for ma, mb in mute_src_intervals:
            oa, ob = max(ma, in_pt), min(mb, out_pt)
            if ob - oa >= 1.5:
                ta, tb = round(_src_to_tl(clip, oa), 3), round(_src_to_tl(clip, ob), 3)
                clip_mutes_tl.append((ta, tb))
                muted_tl_intervals.append((ta, tb))
                muted_summary.append({"clip": a_clip["id"], "tl_start": ta, "tl_end": tb, "dur": round(tb - ta, 2)})

        if clip_mutes_tl:
            a_start = float(a_clip["start"])
            a_end = a_start + float(a_clip["duration"])
            keys = _build_volume_keyframes(clip_mutes_tl, a_start, a_end, fade_sec=float(args.fade_sec))
            ops_2.append(
                {
                    "tool": "set_keyframes",
                    "args": {
                        "clip": a_clip["id"],
                        "interpolation": "linear",
                        "replace": True,
                        "keys": {"volume": keys},
                    },
                }
            )

    for bm in bookmarks_added:
        ops_2.append({"tool": "add_bookmark", "args": {"at": bm["at"], "label": bm["label"]}})

    # 3. Free To Use Music Fill (Audio 3) & Driving Auto-Ducking + SFX (Audio 4)
    fill_tracks: list[dict[str, Any]] = []
    sfx_whoosh: dict[str, Any] | None = None
    if isinstance(fill_assets_raw, dict):
        fill_tracks = fill_assets_raw.get("tracks") or fill_assets_raw.get("music") or []
        sfx_whoosh = fill_assets_raw.get("whoosh_sfx") or fill_assets_raw.get("sfx_whoosh")
    elif isinstance(fill_assets_raw, list):
        fill_tracks = fill_assets_raw

    driving_tl: list[tuple[float, float]] = []
    raw_drv = driving_raw.get("ranges", driving_raw) if isinstance(driving_raw, dict) else driving_raw
    for dr in raw_drv or []:
        if isinstance(dr, dict) and "start" in dr and "end" in dr:
            driving_tl.append((float(dr["start"]), float(dr["end"])))
        elif isinstance(dr, (list, tuple)) and len(dr) >= 2:
            driving_tl.append((float(dr[0]), float(dr[1])))

    used_tracks: dict[str, dict[str, Any]] = {}
    music_fill_segments: list[dict[str, Any]] = []

    if fill_tracks and (muted_tl_intervals or driving_tl):
        # Determine target audio lane index (place first clip on new Audio 3 lane)
        existing_tracks = inspect_data.get("tracks", [])
        audio3_idx = len(existing_tracks)
        for tr in existing_tracks:
            if tr.get("type") == "audio" and "3" in str(tr.get("label", "")):
                audio3_idx = int(tr["i"])
                break

        # Combine muted gaps (priority) and uncovered driving intervals
        muted_merged = merge_intervals(muted_tl_intervals, max_gap=0.4)
        driving_only = subtract_intervals(merge_intervals(driving_tl, max_gap=1.0), muted_merged, min_len=3.0)
        all_fills: list[tuple[float, float, bool]] = [(a, b, False) for a, b in muted_merged] + [
            (a, b, True) for a, b in driving_only
        ]
        all_fills.sort(key=lambda x: x[0])

        # Maintain continuous playback cursor per track id so music flows within scenes
        track_cursor: dict[str, float] = {}
        need_new_track = audio3_idx >= len(existing_tracks)
        if need_new_track:
            ops_3.append({"tool": "add_track", "args": {"type": "audio"}})
            # add_track prepends at index 0, shifting existing tracks down by +1;
            # therefore target track 0 for newly prepended Audio lane!
            target_music_track = 0
        else:
            target_music_track = audio3_idx

        # Primary dialogue track index (shifts by any prepended audio lanes)
        audio_tracks = [t for t in existing_tracks if t.get("type") == "audio"]
        base_dialogue_track = int(audio_tracks[0]["i"]) if audio_tracks else 1
        extra_audio_tracks = (1 if need_new_track else 0) + (1 if (sfx_whoosh and time_jump_cuts) else 0)
        dialogue_track_post = base_dialogue_track + extra_audio_tracks

        for seg_idx, (a, b, is_driving) in enumerate(all_fills):
            rem_start, rem_end = a, b
            while rem_end - rem_start >= 1.5:
                tr_meta = _pick_fill_track(
                    fill_tracks,
                    rem_start,
                    rem_end - rem_start,
                    hint_mood="driving" if is_driving else None,
                    idx_hint=seg_idx,
                )
                tid = tr_meta["id"]
                tdur = float(tr_meta.get("dur", 150.0))
                cursor = track_cursor.get(tid, 4.0)
                if cursor >= tdur - 4.0:
                    cursor = 4.0
                avail = tdur - cursor
                piece_dur = min(rem_end - rem_start, avail)
                piece_end = round(rem_start + piece_dur, 3)
                s_in = round(cursor, 3)
                s_out = round(cursor + piece_dur, 3)
                track_cursor[tid] = s_out

                used_tracks[tid] = tr_meta
                music_fill_segments.append(
                    {
                        "tl_start": round(rem_start, 3),
                        "tl_end": piece_end,
                        "track_title": tr_meta.get("title", tid),
                        "in": s_in,
                        "out": s_out,
                        "volume": float(tr_meta.get("volume", 0.30)),
                        "driving_duck": is_driving,
                    }
                )
                ops_3.append(
                    {
                        "tool": "insert_clip",
                        "args": {
                            "asset": tid,
                            "track": target_music_track,
                            "at": round(rem_start, 3),
                            "in": s_in,
                            "out": s_out,
                            "ripple": False,
                        },
                    }
                )
                rem_start = piece_end

        ops_3_notes = {
            "post_insert_instructions": (
                "After executing batch_3_music_sfx.json, read done[].result.id from the apply output "
                "and apply set_volume, set_fade (in/out ~0.25s), set_fade_curve (equalPower/both), "
                f"and duck_under (over_track={dialogue_track_post}, amount=0.25) on driving segments."
            ),
            "music_fill_segments": music_fill_segments,
        }
        write_json(out_dir / "music_fill_metadata.json", ops_3_notes)

    if sfx_whoosh and time_jump_cuts:
        sfx_id = sfx_whoosh["id"] if isinstance(sfx_whoosh, dict) else str(sfx_whoosh)
        sfx_dur = float(sfx_whoosh.get("dur", 0.8)) if isinstance(sfx_whoosh, dict) else 0.8
        ops_3.append({"tool": "add_track", "args": {"type": "audio"}})
        for t_cut in time_jump_cuts:
            at_t = max(0.0, round(t_cut - min(0.15, sfx_dur * 0.3), 3))
            ops_3.append(
                {
                    "tool": "insert_clip",
                    "args": {
                        "asset": sfx_id,
                        "track": 0,
                        "at": at_t,
                        "in": 0.0,
                        "out": round(min(sfx_dur, 0.55), 3),
                        "ripple": False,
                    },
                }
            )

    write_json(out_dir / "batch_2_transitions_mutes.json", {"ops": ops_2})
    write_json(out_dir / "batch_2_transitions_mutes_dry.json", {"ops": ops_2, "dry_run": True})
    write_json(out_dir / "batch_3_music_sfx.json", {"ops": ops_3})
    write_json(out_dir / "batch_3_music_sfx_dry.json", {"ops": ops_3, "dry_run": True})

    # 4. YouTube Credits, Chapters & Edit Report
    credits_lines = ["Music Credits (Free To Use / CutWire Drift):"]
    for tr in used_tracks.values():
        attrib = tr.get("attribution") or f"{tr.get('title', 'Track')} by {tr.get('artist', 'Artist')} (freetouse.com)"
        lurl = tr.get("license_url") or "https://freetouse.com/license"
        credits_lines.append(f"- {attrib} | License: {lurl}")
    if len(credits_lines) == 1:
        credits_lines.append("- (No external Free To Use tracks placed yet.)")
    write_text(out_dir / "youtube_credits.txt", "\n".join(credits_lines) + "\n")

    chapter_lines = ["00:00 Intro / Start"]
    for idx, clip in enumerate(vclips[1:], start=1):
        t_start = float(clip.get("start", 0.0))
        if t_start >= 15.0:
            chapter_lines.append(f"{format_youtube_timestamp(t_start)} Part {idx + 1} ({clip.get('name', 'Segment')})")
    write_text(out_dir / "chapters.txt", "\n".join(chapter_lines) + "\n")

    report = [
        "# CutWire Drift — YouTube Edit Report",
        "",
        f"- **Timeline clips inspected:** {len(vclips)}",
        f"- **Time-jump transitions planned:** {len(time_jump_cuts)} (`{args.transition_kind}`, `{args.transition_dur}s`)",
        f"- **Copyright music mute windows:** {len(muted_summary)} ({sum(m['dur'] for m in muted_summary):.1f}s total)",
        f"- **Free To Use fill clips planned:** {len(music_fill_segments)}",
        f"- **Review bookmarks queued:** {len(bookmarks_added)}",
        "",
        "## Review Bookmarks",
    ]
    if bookmarks_added:
        for bm in bookmarks_added:
            report.append(f"- `{format_youtube_timestamp(bm['at'])}` ({bm['at']}s) — **{bm['label']}**: {bm['reason']}")
    else:
        report.append("- None.")
    report.extend(["", "## Music Credits", *credits_lines[1:]])
    write_text(out_dir / "edit_report.md", "\n".join(report) + "\n")

    print(
        f"[plan-youtube] stage={args.stage} ops2={len(ops_2)} ops3={len(ops_3)} "
        f"mutes={len(muted_summary)} bookmarks={len(bookmarks_added)} -> {out_dir}"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: find-highlights
# ---------------------------------------------------------------------------


HYPE_PATTERN = re.compile(
    r"(!|\?|(?:j[aeiou]){2,}|"
    r"\b(oh my god|omg|no way|what|wait|bro|dude|holy|clutch|nice|lets go|let's go|"
    r"haha|lmao|wtf|careful|watch out|run|cuidado|corre|qué|no manches|vamos|ay|ala|ostia|"
    r"hostia|ostras|dios|no puede ser|no me lo creo|madre mía|increíble|épico|jaja|jajaja|jsjs|ajaj)\b)",
    re.IGNORECASE,
)


def robust_z_mad(values: Sequence[float], window: int = 180) -> list[float]:
    """Compute local Median/MAD robust z-scores clipped to [-3, 6] per element."""
    import statistics

    n = len(values)
    if n == 0:
        return []
    half = max(1, int(window) // 2)
    out: list[float] = []
    for i in range(n):
        win = [float(x) for x in values[max(0, i - half) : min(n, i + half + 1)]]
        med = statistics.median(win)
        mad = statistics.median([abs(x - med) for x in win]) * 1.4826 + 1e-6
        z = (float(values[i]) - med) / mad
        out.append(round(max(-3.0, min(6.0, z)), 4))
    return out


def _audio_z_per_second(audio_path: Path, n_bins: int) -> list[float]:
    """Extract normalized [0..1] per-second loudness peak boost via robust Median/MAD z-scores."""
    import numpy as np

    cleanup_f32 = False
    if audio_path.suffix.lower() == ".f32":
        f32_path = audio_path
    else:
        tmp = tempfile.NamedTemporaryFile(prefix="drift_hl_", suffix=".f32", delete=False)
        tmp.close()
        cleanup_f32 = True
        f32_path = extract_f32_audio(audio_path, tmp.name, sample_rate=YAMNET_SR)

    try:
        wav = np.fromfile(str(f32_path), dtype=np.float32)
        db_vals: list[float] = []
        step_half = YAMNET_SR // 2  # 0.5 s frames aggregated by max/second
        total_half = len(wav) // step_half
        half_dbs = []
        for idx in range(total_half):
            chunk = wav[idx * step_half : (idx + 1) * step_half]
            rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-12))
            half_dbs.append(20.0 * math.log10(rms))
        for sec_i in range(n_bins):
            pair = half_dbs[sec_i * 2 : sec_i * 2 + 2]
            db_vals.append(max(pair) if pair else -90.0)
        z_vals = robust_z_mad(db_vals, window=180)
        return [round(min(1.0, max(0.0, z) / 4.0), 4) for z in z_vals]
    finally:
        if cleanup_f32 and f32_path.exists():
            f32_path.unlink(missing_ok=True)


def _snap_boundary(t: float, boundaries: Sequence[float], max_shift: float = 3.0) -> float:
    best = t
    best_dist = max_shift + 1e-6
    for b in boundaries:
        d = abs(b - t)
        if d <= max_shift and d < best_dist:
            best = b
            best_dist = d
    return round(best, 3)


def cmd_find_highlights(args: argparse.Namespace) -> int:
    """Score full gameplay transcript + optional 16kHz audio z-scores + activity/scenes for >=5 Reels (>60s)."""
    t_doc = read_json_auto(args.transcript_file)
    segments = t_doc.get("segments", [])
    dur = float(t_doc.get("duration_sec") or 0.0)
    if dur <= 0.0 and segments:
        dur = max(float(s["end"]) for s in segments)
    if dur <= 0.0:
        raise ValueError("Transcript has zero duration and no segments.")

    n_bins = max(1, int(math.ceil(dur)))
    speech_energy = [0.0] * n_bins
    hype_energy = [0.0] * n_bins
    visual_energy = [0.0] * n_bins
    audio_energy = [0.0] * n_bins
    boundaries: list[float] = [0.0, dur]
    sentence_ends: list[float] = [0.0, dur]

    for seg in segments:
        sa, sb = float(seg["start"]), float(seg["end"])
        boundaries.extend([sa, sb])
        txt = str(seg.get("text", "")).strip()
        if txt.endswith((".", "!", "?", "…")):
            sentence_ends.append(sb)
        nsp = float(seg.get("no_speech_prob", 0.5))
        if nsp > 0.75 or not txt:
            continue
        words = seg.get("words") or txt.split()
        span = max(0.5, sb - sa)
        w_rate = min(4.0, len(words) / span) / 4.0
        hype_hits = len(HYPE_PATTERN.findall(txt))
        hype_val = min(1.0, hype_hits * 0.35)
        for sec_i in range(max(0, int(sa)), min(n_bins, int(math.ceil(sb)))):
            speech_energy[sec_i] = max(speech_energy[sec_i], 0.5 + 0.5 * w_rate)
            hype_energy[sec_i] = max(hype_energy[sec_i], hype_val)

    audio_z_active = False
    if getattr(args, "audio_f32", None):
        az = _audio_z_per_second(Path(args.audio_f32), n_bins)
        for sec_i in range(n_bins):
            audio_energy[sec_i] = max(audio_energy[sec_i], az[sec_i])
        audio_z_active = True

    if args.activity_file:
        act = read_json_auto(args.activity_file)
        for pk in act.get("peaks", []):
            t = float(pk.get("t", pk.get("time", -1)))
            if 0 <= int(t) < n_bins:
                visual_energy[int(t)] = max(visual_energy[int(t)], min(1.0, float(pk.get("change", 30.0)) / 60.0))
                audio_energy[int(t)] = max(audio_energy[int(t)], float(pk.get("audio", 0.5)))
        for idx_s, sample in enumerate(act.get("samples", [])):
            if isinstance(sample, dict):
                t = float(sample.get("t", idx_s))
                if 0 <= int(t) < n_bins:
                    visual_energy[int(t)] = max(
                        visual_energy[int(t)], min(1.0, float(sample.get("change", 0.0)) / 50.0)
                    )
                    audio_energy[int(t)] = max(audio_energy[int(t)], float(sample.get("audio", 0.0)))

    if args.scenes_file:
        sc = read_json_auto(args.scenes_file)
        for shot in sc.get("scenes", sc.get("shots", [])):
            t = float(shot.get("timeline_start", shot.get("start", -1)))
            if 0 <= int(t) < n_bins:
                visual_energy[int(t)] = max(visual_energy[int(t)], min(1.0, float(shot.get("score", 0.6))))

    boundaries = sorted(set(round(b, 2) for b in boundaries))
    sentence_ends = sorted(set(round(b, 2) for b in sentence_ends))
    snap_targets = sentence_ends if len(sentence_ends) >= 6 else boundaries
    total_per_sec = [
        0.30 * speech_energy[i] + 0.30 * hype_energy[i] + 0.15 * visual_energy[i] + 0.25 * audio_energy[i]
        for i in range(n_bins)
    ]

    min_d = float(args.min_dur)
    max_d = max(min_d, float(args.max_dur))
    candidates: list[dict[str, Any]] = []

    def _score_window(sa: float, sb: float) -> dict[str, Any] | None:
        if sb <= sa:
            return None
        i0, i1 = max(0, int(sa)), min(n_bins, int(math.ceil(sb)))
        if i1 <= i0:
            return None
        span_len = max(1, i1 - i0)
        mean_score = sum(total_per_sec[i0:i1]) / span_len
        peak_density = sum(sorted(total_per_sec[i0:i1], reverse=True)[: max(1, span_len // 4)]) / max(
            1, span_len // 4
        )
        hype_frac = sum(1 for x in hype_energy[i0:i1] if x > 0.2) / span_len
        head_mean = sum(total_per_sec[i0 : min(i1, i0 + 8)]) / max(1, min(8, span_len))
        tail_mean = sum(total_per_sec[max(i0, i1 - 8) : i1]) / max(1, min(8, span_len))
        edge_bonus = 0.15 * (head_mean + tail_mean)

        win_segs = [s for s in segments if s["end"] > sa and s["start"] < sb]
        words_tok = [w.lower() for s in win_segs for w in str(s.get("text", "")).split() if w.strip()]
        diversity = len(set(words_tok)) / max(1, len(words_tok))
        mult = 1.0
        if len(words_tok) < 15:
            mult *= 0.55  # Penalize sparse/silent ambient spans
        elif len(words_tok) > 25 and diversity < 0.28:
            mult *= 0.65  # Penalize looped radio/TV background dialogue

        base = mean_score + 0.35 * peak_density + 0.25 * hype_frac + edge_bonus
        peak_sec = max(range(i0, i1), key=lambda sec_i: total_per_sec[sec_i])
        return {
            "start": round(sa, 3),
            "end": round(sb, 3),
            "duration": round(sb - sa, 3),
            "peak_sec": int(peak_sec),
            "score": round(base * mult, 3),
        }

    if dur <= min_d:
        scored = _score_window(0.0, dur)
        if scored:
            candidates.append(scored)
    else:
        win_lengths = sorted({min_d, round((min_d + max_d) / 2.0, 1), max_d})
        seen_spans: set[tuple[float, float]] = set()
        raw_ranges: list[tuple[float, float]] = []

        # 1) Sliding windows across full timeline
        for wlen in win_lengths:
            if wlen > dur:
                continue
            for st in range(0, max(1, int(dur - wlen) + 1), 5):
                raw_ranges.append((float(st), min(dur, float(st) + wlen)))

        # 2) Peak-aligned windows placing major climax around ~67% (2/3 build-up + 1/3 payoff)
        top_peaks = sorted(range(n_bins), key=lambda sec_i: total_per_sec[sec_i], reverse=True)
        picked_peaks: list[int] = []
        for psec in top_peaks:
            if total_per_sec[psec] <= 0.25:
                break
            if any(abs(psec - prev) < 25 for prev in picked_peaks):
                continue
            picked_peaks.append(psec)
            for wlen in win_lengths:
                if wlen <= dur:
                    sa_raw = max(0.0, psec - 0.66 * wlen)
                    sb_raw = min(dur, sa_raw + wlen)
                    sa_raw = max(0.0, sb_raw - wlen)
                    raw_ranges.append((sa_raw, sb_raw))
            if len(picked_peaks) >= args.max_count * 3:
                break

        for st, en in raw_ranges:
            sa = _snap_boundary(st, snap_targets, max_shift=3.5)
            sb = _snap_boundary(en, snap_targets, max_shift=3.5)
            if sb - sa < min_d:
                sb = min(dur, sa + min_d)
            if sb - sa > max_d:
                sb = sa + max_d
            key = (round(sa, 2), round(sb, 2))
            if key in seen_spans or sb - sa < min_d - 0.5:
                continue
            seen_spans.add(key)
            scored = _score_window(sa, sb)
            if scored:
                candidates.append(scored)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    min_gap = float(args.min_gap)

    for pass_gap, min_only in ((min_gap, False), (0.0, False), (0.0, True)):
        pool = [c for c in candidates if not min_only or c["duration"] <= min_d + 5.0]
        if min_only:
            selected = []
        for cand in pool:
            if len(selected) >= args.max_count:
                break
            ca, cb = cand["start"], cand["end"]
            if any(not (cb + pass_gap <= s["start"] or ca >= s["end"] + pass_gap) for s in selected):
                continue
            selected.append(cand)
        if len(selected) >= args.min_count:
            break

    selected.sort(key=lambda c: c["start"])
    highlights: list[dict[str, Any]] = []
    md_lines = [
        "# Reel Highlight Candidates",
        "",
        "Review transcript context before cutting: pick stories with a strong opening hook (0-2 s), zero dead air, clean sentence endings, and a clear payoff.",
        "",
    ]

    for idx, sel in enumerate(selected, start=1):
        sa, sb = sel["start"], sel["end"]
        sub_segs = [s for s in segments if s["end"] > sa and s["start"] < sb]
        full_text = " ".join(s.get("text", "").strip() for s in sub_segs if s.get("text", "").strip()).strip()
        preview = full_text[:220]

        i0, i1 = max(0, int(sa)), min(n_bins, int(math.ceil(sb)))
        max_ae = max(audio_energy[i0:i1]) if i1 > i0 else 0.0
        max_ve = max(visual_energy[i0:i1]) if i1 > i0 else 0.0
        hype_hits = len(HYPE_PATTERN.findall(full_text))
        reasons = []
        if audio_z_active and max_ae >= 0.5:
            reasons.append(f"voice_z +{max_ae * 4.0:.1f}")
        elif max_ae >= 0.5:
            reasons.append("audio_peak")
        if hype_hits:
            reasons.append(f"{hype_hits} hype/laugh cues")
        if max_ve >= 0.5:
            reasons.append("scene/activity peak")

        # Editorial default: subtle entry accent only (no mid-clip effect spam unless --with-mid-cues)
        zoom_cues: list[dict[str, Any]] = [
            {
                "kind": "entry",
                "rel_time": 0.0,
                "end_rel": 0.85,
                "abs_time": round(sa, 3),
                "intensity": 0.35,
                "scale_from": 1.06,
                "scale_target": 1.0,
            }
        ]
        sfx_cues: list[dict[str, Any]] = [
            {
                "kind": "entry",
                "rel_time": 0.0,
                "abs_time": round(sa, 3),
                "sfx_kind": "whoosh",
            }
        ]
        if getattr(args, "with_mid_cues", False):
            peak_secs = sorted(range(i0, i1), key=lambda sec_i: total_per_sec[sec_i], reverse=True)
            for ps in peak_secs:
                if len(zoom_cues) >= 5:
                    break
                if ps - sa < 2.0 or sb - ps < 2.0:
                    continue
                if any(abs(ps - z["abs_time"]) < 6.5 for z in zoom_cues):
                    continue
                intensity = round(min(1.0, total_per_sec[ps]), 2)
                zoom_cues.append(
                    {
                        "kind": "mid",
                        "rel_time": round(ps - sa, 2),
                        "abs_time": float(ps),
                        "intensity": intensity,
                        "scale_target": round(1.08 + 0.10 * intensity, 3),
                    }
                )
                sfx_kind = "impact" if hype_energy[ps] >= 0.35 or audio_energy[ps] >= 0.6 else "whoosh"
                sfx_cues.append(
                    {
                        "kind": "mid",
                        "rel_time": round(max(0.0, ps - sa - 0.12), 2),
                        "abs_time": round(max(sa, ps - 0.12), 2),
                        "sfx_kind": sfx_kind,
                    }
                )
            zoom_cues.sort(key=lambda z: z["rel_time"])
            sfx_cues.sort(key=lambda z: z["rel_time"])

        highlights.append(
            {
                "id": f"reel_{idx}",
                "rank": idx,
                "start": sa,
                "end": sb,
                "duration": round(sb - sa, 3),
                "peak_abs_time": sel.get("peak_sec"),
                "peak_pct": round((float(sel.get("peak_sec", sa)) - sa) / max(1.0, sb - sa), 2),
                "score": sel["score"],
                "reasons": reasons,
                "bookmark_label": f"Reel #{idx} ({round(sb - sa, 1)}s)",
                "transcript_preview": preview,
                "zoom_cues": zoom_cues,
                "sfx_cues": sfx_cues,
            }
        )
        md_lines.extend(
            [
                f"## Reel #{idx} | `{format_youtube_timestamp(sa)}–{format_youtube_timestamp(sb)}` ({sb - sa:.1f}s) | score={sel['score']} | {', '.join(reasons) or 'speech'}",
                f"- Source seconds: `{sa} .. {sb}` | Climax at `{int(highlights[-1]['peak_pct'] * 100)}%`",
                f"- Transcript: {full_text or '(no speech detected)'}",
                "",
            ]
        )

    warning = None
    if len(highlights) < args.min_count:
        warning = (
            f"Source duration ({dur:.1f}s) yielded {len(highlights)} non-overlapping "
            f">={min_d:.0f}s clips (requested >= {args.min_count})."
        )

    out_path = Path(args.output)
    md_path = Path(args.summary_md) if getattr(args, "summary_md", None) else out_path.with_suffix(".md")
    write_text(md_path, "\n".join(md_lines))

    payload = {
        "ok": True,
        "source_duration": round(dur, 3),
        "count": len(highlights),
        "summary_md": str(md_path),
        "warning": warning,
        "highlights": highlights,
    }
    write_json(out_path, payload)
    print(f"[find-highlights] count={len(highlights)} min_dur={min_d}s md={md_path} -> {out_path}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: build-reel-subs
# ---------------------------------------------------------------------------


def _load_ranges(ranges_file: str | Path) -> list[dict[str, Any]]:
    data = read_json_auto(ranges_file)
    if isinstance(data, dict):
        items = data.get("highlights") or data.get("ranges") or []
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(f"Unsupported ranges format in '{ranges_file}'.")
    out: list[dict[str, Any]] = []
    for idx, r in enumerate(items, start=1):
        out.append(
            {
                "id": str(r.get("id") or f"reel_{idx}"),
                "start": float(r["start"]),
                "end": float(r["end"]),
            }
        )
    return out


def _map_word_to_timeline(
    w_start: float,
    w_end: float,
    rng: dict[str, Any],
    seq_offset: float,
    timeline_mode: str,
    inspect_clips: Sequence[dict[str, Any]],
) -> tuple[float, float] | None:
    if inspect_clips:
        mid = 0.5 * (w_start + w_end)
        for c in inspect_clips:
            in_pt, out_pt = float(c["inPoint"]), float(c["outPoint"])
            if in_pt - 0.05 <= mid <= out_pt + 0.05:
                ca = max(in_pt, w_start)
                cb = min(out_pt, max(ca + 0.05, w_end))
                return (_src_to_tl(c, ca), _src_to_tl(c, cb))
        return None
    if timeline_mode == "source":
        return (w_start, w_end)
    return (seq_offset + (w_start - rng["start"]), seq_offset + (w_end - rng["start"]))


def _normalize_match_token(token: str) -> str:
    norm = unicodedata.normalize("NFD", token.lower())
    stripped = "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9ñ]", "", stripped) or token.lower()


def align_words_to_script(
    script_text: str,
    whisper_words: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Align clean/corrected script tokens onto Whisper word timestamps (adapted from voz.py)."""
    tokens = re.findall(r"\S+", script_text.strip())
    if not tokens:
        return []
    if not whisper_words:
        raise ValueError("Cannot align script without reference Whisper word timestamps.")

    has_tl = "tl_start" in whisper_words[0]
    sk = "tl_start" if has_tl else "start"
    ek = "tl_end" if has_tl else "end"
    wk = "word" if "word" in whisper_words[0] else "w"

    a = [_normalize_match_token(t) for t in tokens]
    b = [_normalize_match_token(str(w.get(wk, ""))) for w in whisper_words]
    spans: list[tuple[float, float] | None] = [None] * len(tokens)

    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                spans[i1 + k] = (float(whisper_words[j1 + k][sk]), float(whisper_words[j1 + k][ek]))
        elif i2 > i1 and j2 > j1:
            s0 = float(whisper_words[j1][sk])
            e0 = max(s0 + 0.05, float(whisper_words[j2 - 1][ek]))
            total_chars = sum(len(tokens[k]) for k in range(i1, i2)) or 1
            acc = 0.0
            for k in range(i1, i2):
                dur = (e0 - s0) * len(tokens[k]) / total_chars
                spans[k] = (s0 + acc, s0 + acc + max(0.04, dur))
                acc += dur

    for k in range(len(tokens)):
        if spans[k] is None:
            prev_end = next((spans[j][1] for j in range(k - 1, -1, -1) if spans[j] is not None), 0.0)
            nxt_start = next(
                (spans[j][0] for j in range(k + 1, len(tokens)) if spans[j] is not None),
                prev_end + 0.3,
            )
            spans[k] = (prev_end, max(prev_end + 0.05, min(nxt_start, prev_end + 0.35)))

    out: list[dict[str, Any]] = []
    base_meta = {k: v for k, v in whisper_words[0].items() if k not in (sk, ek, wk)}
    for k, tok in enumerate(tokens):
        sa, sb = spans[k]  # type: ignore[misc]
        item = dict(base_meta)
        item[sk] = round(float(sa), 3)
        item[ek] = round(max(float(sa) + 0.04, float(sb)), 3)
        item[wk] = tok
        out.append(item)
    return out


def _group_words_into_cues(
    words: Sequence[dict[str, Any]],
    words_per_cue: int,
    max_cue_sec: float,
    min_cue_sec: float,
) -> list[dict[str, Any]]:
    """Group mapped words into punchy 1-3 word karaoke-friendly cues with zero overlaps."""
    cues: list[dict[str, Any]] = []
    buf: list[dict[str, Any]] = []

    def _flush() -> None:
        if not buf:
            return
        start = float(buf[0]["tl_start"])
        end = max(start + min_cue_sec, float(buf[-1]["tl_end"]))
        text = " ".join(w["word"] for w in buf).strip()
        if text:
            cues.append(
                {
                    "reel_id": buf[0]["reel_id"],
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "text": text,
                }
            )
        buf.clear()

    for w in words:
        if buf:
            gap = w["tl_start"] - buf[-1]["tl_end"]
            span = w["tl_end"] - buf[0]["tl_start"]
            same_reel = w["reel_id"] == buf[0]["reel_id"]
            if not same_reel or len(buf) >= words_per_cue or gap > 0.35 or span > max_cue_sec:
                _flush()
        buf.append(w)
        if re.search(r"[.,;:!?…]$", w["word"]):
            _flush()
    _flush()

    # Enforce strict non-overlapping ascending timestamps
    for i in range(1, len(cues)):
        if cues[i]["start"] <= cues[i - 1]["end"]:
            cues[i - 1]["end"] = round(max(cues[i - 1]["start"] + 0.12, cues[i]["start"] - 0.005), 3)
            if cues[i]["start"] <= cues[i - 1]["end"]:
                cues[i]["start"] = round(cues[i - 1]["end"] + 0.005, 3)
                cues[i]["end"] = round(max(cues[i]["start"] + min_cue_sec, cues[i]["end"]), 3)

    for idx, c in enumerate(cues, start=1):
        c["index"] = idx
    return cues


def _apply_translations(
    cues: Sequence[dict[str, Any]],
    trans_raw: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map Spanish translations (and optional cleaned EN copy) onto base cues."""
    by_idx: dict[int, str] = {}
    en_override_by_idx: dict[int, str] = {}
    seg_list: list[dict[str, Any]] = []

    if isinstance(trans_raw, dict):
        if "cues" in trans_raw and isinstance(trans_raw["cues"], list):
            trans_items = trans_raw["cues"]
        else:
            trans_items = [{"index": int(k), "text_es": str(v)} for k, v in trans_raw.items() if str(k).isdigit()]
    elif isinstance(trans_raw, list):
        trans_items = trans_raw
    else:
        raise ValueError("Unsupported --translated-json format.")

    for item in trans_items:
        if not isinstance(item, dict):
            continue
        if "index" in item:
            idx = int(item["index"])
            by_idx[idx] = str(item.get("text_es") or item.get("text") or "").strip()
            clean_en = str(item.get("text_en_clean") or item.get("text_en") or "").strip()
            if clean_en:
                en_override_by_idx[idx] = clean_en
        elif "start" in item and "end" in item:
            seg_list.append(
                {
                    "start": float(item["start"]),
                    "end": float(item["end"]),
                    "text_es": str(item.get("text_es") or item.get("text") or "").strip(),
                }
            )

    clean_en_cues = [
        {
            "index": c["index"],
            "reel_id": c["reel_id"],
            "start": c["start"],
            "end": c["end"],
            "text": en_override_by_idx.get(c["index"], c["text"]),
        }
        for c in cues
    ]

    es_cues: list[dict[str, Any]] = []
    if by_idx:
        for c in clean_en_cues:
            es_cues.append(
                {
                    "index": c["index"],
                    "reel_id": c["reel_id"],
                    "start": c["start"],
                    "end": c["end"],
                    "text": by_idx.get(c["index"], c["text"]),
                }
            )
        return clean_en_cues, es_cues

    # Fallback: distribute segment-level Spanish phrases proportionally across cues inside [start, end]
    assigned: dict[int, list[str]] = {c["index"]: [] for c in cues}
    for seg in seg_list:
        matched = [c for c in cues if c["end"] > seg["start"] and c["start"] < seg["end"]]
        if not matched or not seg["text_es"]:
            continue
        words = seg["text_es"].split()
        n_c = len(matched)
        per = max(1, math.ceil(len(words) / n_c))
        for k, mc in enumerate(matched):
            chunk = " ".join(words[k * per : (k + 1) * per]).strip()
            if chunk:
                assigned[mc["index"]].append(chunk)

    for c in clean_en_cues:
        txt = " ".join(assigned[c["index"]]).strip() or c["text"]
        es_cues.append(
            {
                "index": c["index"],
                "reel_id": c["reel_id"],
                "start": c["start"],
                "end": c["end"],
                "text": txt,
            }
        )
    return clean_en_cues, es_cues


def _build_per_reel_clip_specs(
    reel_spans: Sequence[dict[str, Any]],
    cues: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group timeline cues into independent per-Reel clip payloads for Drift Option C."""
    by_reel: dict[str, list[dict[str, Any]]] = {}
    for c in cues:
        by_reel.setdefault(str(c["reel_id"]), []).append(c)

    out: list[dict[str, Any]] = []
    for idx, sp in enumerate(reel_spans, start=1):
        rid = str(sp["reel_id"])
        r_start = round(float(sp["tl_start"]), 3)
        r_dur = round(max(0.5, float(sp["duration"])), 3)
        r_end = round(r_start + r_dur, 3)
        clip_cues: list[dict[str, Any]] = []
        for c in by_reel.get(rid, []):
            ca = max(r_start, round(float(c["start"]), 3))
            cb = min(r_end, round(float(c["end"]), 3))
            if cb - ca >= 0.05 and str(c.get("text", "")).strip():
                clip_cues.append({"start": ca, "end": cb, "text": str(c["text"]).strip()})
        out.append(
            {
                "reel_id": rid,
                "reel_index": idx,
                "start": r_start,
                "end": r_end,
                "duration": r_dur,
                "cues": clip_cues,
            }
        )
    return out


def _write_srt(path: Path, cues: Sequence[dict[str, Any]]) -> None:
    blocks = []
    for c in cues:
        blocks.append(
            f"{c['index']}\n"
            f"{format_srt_timestamp(c['start'])} --> {format_srt_timestamp(c['end'])}\n"
            f"{c['text']}\n"
        )
    write_text(path, "\n".join(blocks))


def cmd_build_reel_subs(args: argparse.Namespace) -> int:
    """Build synchronized EN/ES SRTs and per-Reel Drift clip cues (with optional script alignment)."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t_doc = read_json_auto(args.transcript_file)
    ranges = _load_ranges(args.ranges_file)

    inspect_clips: list[dict[str, Any]] = []
    if args.inspect_file:
        insp = read_json_auto(args.inspect_file)
        for tr in insp.get("tracks", []):
            if tr.get("type") == "video":
                inspect_clips.extend(tr.get("items", []))
                break
        inspect_clips.sort(key=lambda c: float(c.get("start", 0.0)))

    corrected_scripts: dict[str, str] = {}
    if getattr(args, "corrected_script", None):
        raw_corr = read_json_auto(args.corrected_script)
        if isinstance(raw_corr, dict):
            corrected_scripts = {str(k): str(v) for k, v in raw_corr.items()}
        elif isinstance(raw_corr, list):
            for idx, item in enumerate(raw_corr, start=1):
                if isinstance(item, dict):
                    rid = str(item.get("reel_id") or item.get("id") or f"reel_{idx}")
                    corrected_scripts[rid] = str(item.get("text") or item.get("script") or "")

    # Collect words (or split segment text proportionally if word list missing)
    all_words: list[dict[str, Any]] = []
    for seg in t_doc.get("segments", []):
        if seg.get("words"):
            for w in seg["words"]:
                txt = str(w.get("word", "")).strip()
                if txt:
                    all_words.append({"start": float(w["start"]), "end": float(w["end"]), "word": txt})
        else:
            tokens = str(seg.get("text", "")).strip().split()
            if not tokens:
                continue
            sa, sb = float(seg["start"]), float(seg["end"])
            dt = max(0.05, (sb - sa) / len(tokens))
            for k, tok in enumerate(tokens):
                all_words.append({"start": sa + k * dt, "end": sa + (k + 1) * dt, "word": tok})

    mapped_words: list[dict[str, Any]] = []
    reel_spans: list[dict[str, Any]] = []
    seq_cursor = 0.0
    for idx, rng in enumerate(ranges):
        ra, rb = rng["start"], rng["end"]
        reel_words: list[dict[str, Any]] = []
        matched_clip = inspect_clips[idx] if idx < len(inspect_clips) else None
        for w in all_words:
            mid = 0.5 * (w["start"] + w["end"])
            if ra <= mid <= rb:
                mapped = _map_word_to_timeline(
                    w["start"],
                    w["end"],
                    rng,
                    seq_offset=seq_cursor,
                    timeline_mode=args.timeline_mode,
                    inspect_clips=inspect_clips,
                )
                if mapped is None:
                    continue
                tl_a, tl_b = mapped
                reel_words.append(
                    {
                        "reel_id": rng["id"],
                        "tl_start": tl_a,
                        "tl_end": max(tl_a + 0.05, tl_b),
                        "word": w["word"],
                    }
                )
        if rng["id"] in corrected_scripts and corrected_scripts[rng["id"]].strip() and reel_words:
            reel_words = align_words_to_script(corrected_scripts[rng["id"]], reel_words)
        mapped_words.extend(reel_words)

        if matched_clip is not None:
            tl_s = float(matched_clip.get("start", seq_cursor))
            dur = float(matched_clip.get("duration", rb - ra))
        elif args.timeline_mode == "source":
            tl_s = ra
            dur = rb - ra
        else:
            tl_s = seq_cursor
            dur = rb - ra
        reel_spans.append({"reel_id": rng["id"], "tl_start": round(tl_s, 3), "duration": round(dur, 3)})
        seq_cursor += (rb - ra) + float(args.gap_sec)

    en_cues = _group_words_into_cues(
        mapped_words,
        words_per_cue=args.words_per_cue,
        max_cue_sec=args.max_cue_sec,
        min_cue_sec=args.min_cue_sec,
    )

    translation_sheet = [
        {"index": c["index"], "reel_id": c["reel_id"], "start": c["start"], "end": c["end"], "text_en": c["text"]}
        for c in en_cues
    ]
    write_json(out_dir / "cues_for_translation.json", translation_sheet)

    es_srt_path: str | None = None
    es_clips_path: str | None = None
    if args.translated_json:
        trans_raw = read_json_auto(args.translated_json)
        en_cues, es_cues = _apply_translations(en_cues, trans_raw)
        es_srt = out_dir / "reels_es.srt"
        _write_srt(es_srt, es_cues)
        es_srt_path = str(es_srt)
        es_clips = _build_per_reel_clip_specs(reel_spans, es_cues)
        es_clips_file = out_dir / "reel_clips_es.json"
        write_json(es_clips_file, {"ok": True, "lang": "es", "reels": es_clips})
        es_clips_path = str(es_clips_file)

    en_srt = out_dir / "reels_en.srt"
    _write_srt(en_srt, en_cues)
    en_clips = _build_per_reel_clip_specs(reel_spans, en_cues)
    en_clips_file = out_dir / "reel_clips_en.json"
    write_json(en_clips_file, {"ok": True, "lang": "en", "reels": en_clips})

    manifest = {
        "ok": True,
        "cue_count": len(en_cues),
        "reel_count": len(reel_spans),
        "reels_en_srt": str(en_srt),
        "reels_es_srt": es_srt_path,
        "reel_clips_en": str(en_clips_file),
        "reel_clips_es": es_clips_path,
        "cues_for_translation": str(out_dir / "cues_for_translation.json"),
    }
    write_json(out_dir / "reel_subs_manifest.json", manifest)
    print(f"[build-reel-subs] cues={len(en_cues)} reels={len(reel_spans)} es={'yes' if es_srt_path else 'pending'} -> {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: apply-reel-subs (Native Option C per-Reel karaoke tracks in Drift)
# ---------------------------------------------------------------------------


def _extract_subtitle_clip_specs(path: str | Path) -> list[dict[str, Any]]:
    data = read_json_auto(path)
    reels = data.get("reels", data) if isinstance(data, dict) else data
    return [r for r in reels if isinstance(r, dict) and r.get("cues")]


def _apply_checked(
    url: str,
    token: str,
    ops: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    atomic: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    args: dict[str, Any] = {"ops": ops}
    if dry_run:
        args["dry_run"] = True
    elif atomic:
        args["atomic"] = True
    res, _, is_ok = _post_mcp_tool(url, token, "apply", args, timeout=timeout)
    if not is_ok or not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(f"Drift apply failed (dry_run={dry_run}): {json.dumps(res, ensure_ascii=False)}")
    return res


def _to_clip_local_cues(cues: Sequence[dict[str, Any]], clip_start: float, clip_dur: float) -> list[dict[str, Any]]:
    """Convert timeline-coordinate cues into clip-local [0..clip_dur] cues required by Drift's player."""
    if not cues:
        return []
    need_shift = clip_start > 0.0 and min(float(c["start"]) for c in cues) >= clip_start - 0.05
    offset = clip_start if need_shift else 0.0
    local: list[dict[str, Any]] = []
    for c in cues:
        sa = max(0.0, round(float(c["start"]) - offset, 3))
        sb = min(clip_dur, round(float(c["end"]) - offset, 3))
        if sb - sa >= 0.05 and str(c.get("text", "")).strip():
            local.append({"start": sa, "end": sb, "text": str(c["text"]).strip()})
    return local


def _populate_drift_subtitle_track(
    url: str,
    token: str,
    reels: Sequence[dict[str, Any]],
    style_cfg: dict[str, Any],
    *,
    staging_start: float,
    preset: str,
    pos_y: float,
    track_label: str,
) -> list[dict[str, Any]]:
    """Create 1 independent subtitle clip per Reel, trim/place, inject clip-local cues, and style cleanly."""
    if not reels:
        return []

    stage_ops = [
        {"tool": "add_subtitle_clip", "args": {"at": round(staging_start + i * 35.0, 3)}}
        for i in range(len(reels))
    ]
    _apply_checked(url, token, stage_ops, dry_run=True)
    res_stage = _apply_checked(url, token, stage_ops, atomic=True)
    created_ids = [d["result"]["id"] for d in res_stage.get("done", [])]
    if len(created_ids) != len(reels):
        raise RuntimeError(f"Expected {len(reels)} subtitle clips for '{track_label}', got {len(created_ids)}")

    polish_ops: list[dict[str, Any]] = []
    summary_items: list[dict[str, Any]] = []
    for cid, reel in zip(created_ids, reels):
        r_start = round(float(reel["start"]), 3)
        r_dur = round(float(reel["duration"]), 3)
        local_cues = _to_clip_local_cues(reel["cues"], r_start, r_dur)
        polish_ops.append({"tool": "set_trim", "args": {"clip": cid, "in": 0.0, "out": r_dur}})
        polish_ops.append({"tool": "move_clip", "args": {"clip": cid, "at": r_start}})
        polish_ops.append({"tool": "set_subtitle_cues", "args": {"clip": cid, "cues": local_cues}})
        if preset and preset != "none":
            polish_ops.append({"tool": "apply_text_preset", "args": {"clip": cid, "preset": preset}})
        polish_ops.append({"tool": "set_text", "args": {"clip": cid, "style": style_cfg}})
        polish_ops.append(
            {"tool": "set_transform", "args": {"clip": cid, "positionX": 540, "positionY": pos_y, "scaleX": 1.0, "scaleY": 1.0}}
        )
        summary_items.append(
            {"clip": cid, "reel_id": reel.get("reel_id"), "start": r_start, "duration": r_dur, "cues": len(local_cues)}
        )

    _apply_checked(url, token, polish_ops, dry_run=True)
    _apply_checked(url, token, polish_ops, atomic=True)
    return summary_items


def cmd_apply_reel_subs(args: argparse.Namespace) -> int:
    """Create independent per-Reel native EN/ES karaoke subtitle tracks inside CutWire Drift."""
    url, token = _resolve_mcp_credentials(args)
    if not url or not token:
        raise RuntimeError("Both --url (or DRIFT_MCP_URL) and --token (or DRIFT_MCP_TOKEN) are required.")

    en_path = args.en_clips
    es_path = args.es_clips
    if args.manifest:
        man = read_json_auto(args.manifest)
        en_path = en_path or man.get("reel_clips_en")
        es_path = es_path or man.get("reel_clips_es")

    en_reels = _extract_subtitle_clip_specs(en_path) if en_path and Path(en_path).exists() else []
    es_reels = _extract_subtitle_clip_specs(es_path) if es_path and Path(es_path).exists() else []
    if not en_reels and not es_reels:
        raise ValueError("No per-Reel subtitle clips found; pass --manifest or --en-clips / --es-clips.")

    tracks_by_lang = []
    if args.primary_lang == "es":
        if en_reels:
            tracks_by_lang.append(("en", en_reels, True))
        if es_reels:
            tracks_by_lang.append(("es", es_reels, False))
    else:
        if es_reels:
            tracks_by_lang.append(("es", es_reels, True))
        if en_reels:
            tracks_by_lang.append(("en", en_reels, False))

    style_cfg: dict[str, Any] = {
        "fontFamily": args.font,
        "pixelSize": int(args.pixel_size),
        "letterSpacing": int(args.letter_spacing),
        "textCase": args.text_case,
        "align": "center",
        "valign": "center",
        "boxEnabled": False,
        "accent": {
            "rule": "karaoke",
            "color": args.accent_color,
            "colorEnabled": True,
            "sizeScale": float(args.size_scale),
        },
    }

    if args.dry_run:
        plan = {
            "ok": True,
            "dry_run": True,
            "preset": args.preset,
            "style": style_cfg,
            "pos_y": args.pos_y,
            "tracks_order": [{"lang": lang, "clips": len(rs), "hidden": hid} for lang, rs, hid in reversed(tracks_by_lang)],
        }
        if args.output:
            write_json(args.output, plan)
        print(f"[apply-reel-subs] dry-run tracks={len(tracks_by_lang)}")
        return 0

    ins, _, ok_ins = _post_mcp_tool(url, token, "inspect", {"clips": False})
    if not ok_ins or not isinstance(ins, dict):
        raise RuntimeError(f"Drift inspect failed: {ins}")
    existing_subs = [t for t in ins.get("tracks", []) if t.get("type") == "subtitle"]
    if existing_subs and not args.allow_extra_tracks:
        raise RuntimeError(
            f"Project already has {len(existing_subs)} subtitle track(s); remove them or pass --allow-extra-tracks."
        )

    staging_base = max(float(ins.get("dur") or 0.0) + 30.0, 600.0)
    applied_tracks: list[dict[str, Any]] = []

    for idx, (lang, reels, should_hide) in enumerate(tracks_by_lang):
        if idx > 0 or existing_subs:
            prep_ops = [{"tool": "add_track", "args": {"type": "subtitle"}}]
            _apply_checked(url, token, prep_ops, dry_run=True)
            _apply_checked(url, token, prep_ops, atomic=True)
        items = _populate_drift_subtitle_track(
            url,
            token,
            reels,
            style_cfg,
            staging_start=staging_base + idx * 1000.0,
            preset=args.preset,
            pos_y=float(args.pos_y),
            track_label=lang,
        )
        applied_tracks.append({"lang": lang, "hidden": should_hide, "clips": items})

    vis_ops = []
    n_tracks = len(applied_tracks)
    # Last appended language sits on Track 0; first appended shifted down to Track n_tracks - 1.
    for pos, entry in enumerate(reversed(applied_tracks)):
        vis_ops.append({"tool": "set_track", "args": {"track": pos, "hidden": bool(entry["hidden"])}})
    if vis_ops:
        _apply_checked(url, token, vis_ops, dry_run=True)
        _apply_checked(url, token, vis_ops, atomic=True)

    result = {
        "ok": True,
        "preset": args.preset,
        "pos_y": float(args.pos_y),
        "tracks": [{"track": i, **entry} for i, entry in enumerate(reversed(applied_tracks))],
        "track_count": n_tracks,
    }
    if args.output:
        write_json(args.output, result)
    print(f"[apply-reel-subs] ok tracks={n_tracks} primary={args.primary_lang}")
    return 0


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the multi-subcommand argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="drift_gameplay_cli.py",
        description="Automation helpers for CutWire Drift gameplay editing.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # mcp
    p_mcp = sub.add_parser("mcp", help="Call a CutWire Drift /mcp JSON-RPC tool.")
    p_mcp.add_argument("--url", help="Drift /mcp URL (or DRIFT_MCP_URL env var)")
    p_mcp.add_argument("--token", help="Drift Bearer token (or DRIFT_MCP_TOKEN env var)")
    p_mcp.add_argument("--tool", required=True, help="MCP tool name (e.g. inspect, apply, toolbox)")
    g_args = p_mcp.add_mutually_exclusive_group()
    g_args.add_argument("--args-file", help="Path to JSON file with tool arguments")
    g_args.add_argument("--args-json", default="{}", help="Inline JSON string for tool arguments")
    p_mcp.add_argument("--timeout", type=int, default=1800, help="HTTP timeout seconds")
    p_mcp.add_argument("--save-images-dir", help="Optional folder to save returned base64 images")
    p_mcp.add_argument("--output", required=True, help="Output JSON file path")
    p_mcp.set_defaults(func=cmd_mcp)

    # scan-pauses
    p_sp = sub.add_parser("scan-pauses", help="Two-phase ffmpeg scan for pause/map/static cuts.")
    p_sp.add_argument("--video", required=True, help="Path to source video")
    p_sp.add_argument("--ref-pause", action="append", default=[], help="Reference PNG for pause/dead screen (repeatable)")
    p_sp.add_argument("--ref-map", action="append", default=[], help="Reference PNG for map screen (repeatable)")
    p_sp.add_argument("--crop-pause", help="Optional w:h:x:y (px or 0..1 fraction) for pause ref comparison")
    p_sp.add_argument("--crop-map", help="Optional w:h:x:y (px or 0..1 fraction) for map ref comparison")
    p_sp.add_argument("--min-pause-sec", type=float, default=2.0)
    p_sp.add_argument("--min-map-sec", type=float, default=5.0)
    p_sp.add_argument("--min-static-sec", type=float, default=3.5)
    p_sp.add_argument("--mae-thresh", type=float, default=18.0)
    p_sp.add_argument("--static-diff-thresh", type=float, default=1.6)
    p_sp.add_argument("--refine-fps", type=float, default=20.0)
    p_sp.add_argument("--output", required=True, help="Output JSON path")
    p_sp.set_defaults(func=cmd_scan_pauses)

    # detect-music
    p_dm = sub.add_parser("detect-music", help="Classify music vs speech windows with YAMNet.")
    g_src = p_dm.add_mutually_exclusive_group(required=True)
    g_src.add_argument("--video", help="Source video path")
    g_src.add_argument("--audio", help="Source audio (.f32, .wav, .mp3) path")
    p_dm.add_argument("--speech-thresh", type=float, default=0.40)
    p_dm.add_argument("--music-thresh", type=float, default=0.30)
    p_dm.add_argument("--sing-thresh", type=float, default=0.15)
    p_dm.add_argument("--min-music-sec", type=float, default=4.0)
    p_dm.add_argument("--min-mute-sec", type=float, default=2.0)
    p_dm.add_argument("--gap-merge-sec", type=float, default=3.0)
    p_dm.add_argument("--speech-pad-sec", type=float, default=0.5)
    p_dm.add_argument("--chunk-sec", type=int, default=600)
    p_dm.add_argument("--keep-f32", help="Save 16kHz mono float32 PCM here for reuse by transcribe")
    p_dm.add_argument("--output", required=True, help="Output JSON path")
    p_dm.set_defaults(func=cmd_detect_music)

    # transcribe
    p_tr = sub.add_parser("transcribe", help="Transcribe windows or full clip with faster-whisper.")
    p_tr.add_argument("--audio", required=True, help="Audio or video file (.f32, .wav, .mp3, .mp4)")
    g_mode = p_tr.add_mutually_exclusive_group(required=True)
    g_mode.add_argument("--windows-file", help="JSON with music_windows / windows list")
    g_mode.add_argument("--full", action="store_true", help="Transcribe entire clip")
    p_tr.add_argument("--model", default="small", help="Whisper model size (tiny, base, small, medium, large-v3-turbo)")
    p_tr.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    p_tr.add_argument("--compute-type", default="auto")
    p_tr.add_argument("--language", default=None, help="Optional ISO language code (e.g. en, es)")
    p_tr.add_argument("--vad-filter", action="store_true", help="Enable Silero VAD filter")
    p_tr.add_argument("--no-speech-threshold", type=float, default=0.7)
    p_tr.add_argument("--output", required=True, help="Output JSON path")
    p_tr.set_defaults(func=cmd_transcribe)

    # plan-youtube
    p_py = sub.add_parser("plan-youtube", help="Generate phased Drift apply batches and YouTube files.")
    p_py.add_argument("--stage", choices=("cuts", "delete-cuts", "post-cuts", "all"), default="post-cuts")
    p_py.add_argument("--inspect-file", required=True, help="Drift inspect({clips:true}) JSON")
    p_py.add_argument("--pauses-file", help="scan-pauses JSON or manifest list")
    p_py.add_argument("--apply-result", help="apply output JSON for --stage delete-cuts")
    p_py.add_argument("--music-file", help="detect-music JSON or manifest list")
    p_py.add_argument("--transcript-file", help="transcribe JSON or manifest list")
    p_py.add_argument("--fill-assets-file", help="JSON with Free To Use tracks and optional whoosh SFX")
    p_py.add_argument("--driving-ranges-file", help="JSON with timeline [start, end] driving spans")
    p_py.add_argument("--transition-kind", default="crossfade")
    p_py.add_argument("--transition-dur", type=float, default=0.30)
    p_py.add_argument("--fade-sec", type=float, default=0.15)
    p_py.add_argument("--music-fade-sec", type=float, default=0.25)
    p_py.add_argument("--sfx-whoosh-volume", type=float, default=0.45)
    p_py.add_argument("--out-dir", required=True, help="Output directory for batches and reports")
    p_py.set_defaults(func=cmd_plan_youtube)

    # find-highlights
    p_fh = sub.add_parser("find-highlights", help="Score and pick >=5 highlights (>60s) for Reels/Shorts.")
    p_fh.add_argument("--transcript-file", required=True, help="Full transcript JSON")
    p_fh.add_argument("--audio-f32", help="Optional 16kHz .f32 or media source for Median/MAD loudness peak z-scores")
    p_fh.add_argument("--activity-file", help="Optional Drift activity() JSON")
    p_fh.add_argument("--scenes-file", help="Optional Drift list_scenes() JSON")
    p_fh.add_argument("--min-dur", type=float, default=60.0)
    p_fh.add_argument("--max-dur", type=float, default=90.0)
    p_fh.add_argument("--min-count", type=int, default=5)
    p_fh.add_argument("--max-count", type=int, default=8)
    p_fh.add_argument("--min-gap", type=float, default=5.0)
    p_fh.add_argument("--summary-md", help="Optional Markdown candidate review output path (defaults beside --output)")
    p_fh.add_argument("--with-mid-cues", action="store_true", help="Also emit internal mid-clip zoom/SFX cues (off by default)")
    p_fh.add_argument("--output", required=True, help="Output JSON path")
    p_fh.set_defaults(func=cmd_find_highlights)

    # build-reel-subs
    p_br = sub.add_parser("build-reel-subs", help="Build dynamic dual EN/ES SRTs and per-Reel Drift clip specs.")
    p_br.add_argument("--transcript-file", required=True, help="Full transcript JSON")
    p_br.add_argument("--ranges-file", required=True, help="highlights.json or ranges JSON")
    p_br.add_argument("--inspect-file", help="Optional arranged Reels inspect JSON for exact timeline mapping")
    p_br.add_argument("--timeline-mode", choices=("sequence", "source"), default="sequence")
    p_br.add_argument("--gap-sec", type=float, default=0.0, help="Gap between sequential clips if no inspect-file")
    p_br.add_argument("--corrected-script", help="Optional JSON mapping reel_id -> clean text for word-timestamp alignment")
    p_br.add_argument("--translated-json", help="JSON with Spanish translations (and optional cleaned text_en)")
    p_br.add_argument("--words-per-cue", type=int, default=3)
    p_br.add_argument("--max-cue-sec", type=float, default=1.6)
    p_br.add_argument("--min-cue-sec", type=float, default=0.25)
    p_br.add_argument("--out-dir", required=True, help="Output directory for .srt and reel_clips_*.json files")
    p_br.set_defaults(func=cmd_build_reel_subs)

    # apply-reel-subs
    p_ar = sub.add_parser("apply-reel-subs", help="Push per-Reel native EN/ES karaoke subtitle tracks into Drift.")
    p_ar.add_argument("--url", help="Drift /mcp URL (or DRIFT_MCP_URL env var)")
    p_ar.add_argument("--token", help="Drift Bearer token (or DRIFT_MCP_TOKEN env var)")
    p_ar.add_argument("--manifest", help="Path to reel_subs_manifest.json from build-reel-subs")
    p_ar.add_argument("--en-clips", help="Optional reel_clips_en.json path")
    p_ar.add_argument("--es-clips", help="Optional reel_clips_es.json path")
    p_ar.add_argument("--primary-lang", choices=("es", "en"), default="es")
    p_ar.add_argument("--preset", default="karaoke-pop")
    p_ar.add_argument("--font", default="Anton")
    p_ar.add_argument("--pixel-size", type=int, default=64)
    p_ar.add_argument("--letter-spacing", type=int, default=2)
    p_ar.add_argument("--text-case", default="upper", choices=("none", "upper", "lower", "title", "sentence"))
    p_ar.add_argument("--accent-color", default="#ffffd400")
    p_ar.add_argument("--size-scale", type=float, default=1.0)
    p_ar.add_argument("--pos-y", type=float, default=1435.0)
    p_ar.add_argument("--allow-extra-tracks", action="store_true")
    p_ar.add_argument("--dry-run", action="store_true")
    p_ar.add_argument("--output", help="Optional JSON summary output path")
    p_ar.set_defaults(func=cmd_apply_reel_subs)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
