---
name: drift-gameplay-editor
description: >-
  Automate video editing in CutWire Drift via MCP in two modes: (1) Long YouTube
  16:9 gameplay (pause/dead-screen cuts with transitions/SFX, copyright-music
  detection and muting preserving dialogue, Free-To-Use music fill + driving
  ducking, bookmarks, and YouTube credits/chapters), and (2) Vertical 9:16
  Reels/Shorts (>60s highlights, dynamic zooms, SFX, and dual EN/ES subtitles).
  Activate when editing gameplay or extracting shorts in Drift.
---

# CutWire Drift Gameplay Editor (`drift-gameplay-editor`)

Automates gameplay editing in **CutWire Drift** via its local `/mcp` JSON-RPC endpoint combined with local `ffmpeg`, `YAMNet`, and `faster-whisper` analysis.

Always read [references/drift-workflows.md](./references/drift-workflows.md) for verified Drift MCP tool patterns, preset/effect IDs, volume/LUFS targets, and dual-subtitle rules before building `apply` batches.

---

## Prerequisites

1. **`uv` (Required Python runner):** Check `uv --version`. If missing, follow the **`uv` skill** setup steps to install `uv` and add it to `PATH`.
   - Always execute the helper CLI directly with `uv run "<skill-dir>/scripts/drift_gameplay_cli.py" ...` (never `uv run python ...`, which bypasses the script's PEP 723 dependency metadata).
2. **`ffmpeg` & `ffprobe`:** Verify `ffmpeg -version` and `ffprobe -version` are on `PATH` before scanning media. If unavailable locally, inform the user and fall back to Drift's built-in `detect_silence`, `activity`, `frames`, and `audio_presence` tools where supported.
3. **Active Drift MCP Session:** Never reuse hardcoded or old URLs/tokens. Ask the user for (or verify from the current prompt/env `DRIFT_MCP_URL`, `DRIFT_MCP_TOKEN`) the active `URL` and Bearer `Token`, then verify connectivity:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py mcp --url "<URL>" --token "<TOKEN>" --tool inspect --args-json '{"clips":true,"detail":true}' --output ./out/inspect_initial.json
   ```

---

## Mode Selector

Inspect `inspect_initial.json` and confirm the workflow mode with the user (or follow the user's explicit request):

- **Mode A — Long Gameplay (YouTube 16:9):** Multi-clip or raw long gameplay needing pause/loading/dead-screen cuts, copyright music detection & muting (preserving dialogue), Free To Use music replacement + driving auto-ducking, transitions/SFX, bookmarks, and YouTube credits/chapters.
- **Mode B — Vertical Reels / Shorts / TikTok (`9:16`, `> 60 s`):** Designed for a new project loaded with already-edited/exported gameplay. Converts canvas to `1080×1920`, extracts `>= 5` viral highlights (`60–90 s`), adds dynamic zooms/effects/SFX, and builds dual `Subtitles EN` + `Subtitles ES` tracks ready for toggling and export.

---

## Mode A — Long Gameplay (YouTube 16:9)

### Step 1: Inspect & Separate Linked Audio
1. Check `inspect_initial.json`: if video clips do not yet have separated audio clips on `audio` lanes, select them (`select_clips`) and call `separate_audio`, then re-save `inspect_separated.json`.

### Step 2: Detect Pause, Loading, Map & Dead-Screen Cuts
1. Inspect representative moments with Drift `frames` / `capture` (and save reference PNGs for pause/map screens if helpful) using `mcp --save-images-dir ./out/frames`.
2. Scan each unique source video file in 2 phases (`nokey` fast pass + `fps=20` edge refinement):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py scan-pauses --video "<video_path>" --ref-pause "<pause_png>" --ref-map "<map_png>" --min-pause-sec 2.0 --output ./out/pauses_v1.json
   ```
3. Visually verify candidate start/end cuts (head/tail trims, pause menus, loading/desktop screens, map screens `>= 5 s`) using `frames({at:[...]})` or `capture({at:...})`.
4. Keep conservative judgment: mark ambiguous short map checks or uncertain idle stretches as review bookmarks rather than cutting blindly.

### Step 3: Detect Copyright Music & Preserve Dialogue
1. Extract 16 kHz mono audio (`--keep-f32` to avoid duplicate extraction) and classify speech/music/singing via YAMNet:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py detect-music --video "<video_path>" --keep-f32 ./out/audio_v1.f32 --output ./out/music_v1.json
   ```
2. Transcribe candidate music windows with word-level timestamps to protect spoken dialogue overlapping music:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py transcribe --audio ./out/audio_v1.f32 --windows-file ./out/music_v1.json --model base --output ./out/transcript_v1.json
   ```
   - If initial model download times out, retry or switch `--model small` / `--model base` as reported by the CLI.
3. Inspect ambiguous song edges or instrumental segments (`frames` / `capture` to check in-game radio/cutscene context). Never mute confirmed dialogue or safe original game score; queue uncertain cases for `add_bookmark` and `edit_report.md`.

### Step 4: Source Free To Use Music & Subtle SFX (Zero Quota)
1. Check `list_assets` in `inspect` first to reuse any tracks/SFX already in the bin.
2. Search/install **only** from `stock.music` (`provider: "freetouse"`, `quota: null`) and `sfx` (`quota: null`, e.g. `motion/whoosh`). **Never** spend `stock.audio` or `stock.video` quotas.
3. Call `market_item` for each selected track to obtain exact `license.attribution` (`Title by Artist (freetouse.com)`) and `license.url`, install with `market_install({"source":"stock.music","id":...,"wait":120})`, and save track metadata (`id`, `title`, `artist`, `attribution`, `license_url`, `dur`, `volume`, `mood`) into `fill_assets.json`. Target `~ -22..-24 LUFS` (typically `volume: 0.26–0.34`).

### Step 5: Plan & Apply in Phased Batches (`dry_run` First)
1. **Stage 1a (`cuts`):** Generate `batch_1a_trims_splits.json` (`set_trim` head/tail + descending `split_clip` ops):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py plan-youtube --stage cuts --inspect-file ./out/inspect_separated.json --pauses-file ./out/pauses_manifest.json --out-dir ./out/plan
   ```
   Dry-run via `mcp --tool apply --args-file ./out/plan/batch_1a_trims_splits_dry.json`, inspect for errors, then apply `batch_1a_trims_splits.json` saving `./out/plan/cuts_apply_out.json`.
2. **Stage 1b (`delete-cuts`):** Resolve the newly split clip UUIDs from `cuts_apply_out.json` and delete confirmed pause/dead-screen slices (`delete_clips` with `ripple: true`):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py plan-youtube --stage delete-cuts --inspect-file ./out/inspect_separated.json --pauses-file ./out/pauses_manifest.json --apply-result ./out/plan/cuts_apply_out.json --out-dir ./out/plan
   ```
   Dry-run then apply `batch_1b_delete_cuts.json`.
3. **Re-inspect:** Capture post-cut timeline state `./out/inspect_post_cuts.json`.
4. **Stage 2 (`post-cuts`):** Generate transitions (`crossfade`/`dip`, `0.25–0.35 s`), monotonic `set_keyframes` volume mutes (`0.15 s` linear ramps), `Audio 3` Free To Use fills (both muted gaps and driving-without-radio ranges with `duck_under`), `Audio 4` subtle `whoosh` SFX, review `add_bookmark` markers, plus `youtube_credits.txt`, `chapters.txt`, and `edit_report.md`:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py plan-youtube --stage post-cuts --inspect-file ./out/inspect_post_cuts.json --pauses-file ./out/pauses_manifest.json --music-file ./out/music_manifest.json --transcript-file ./out/transcript_manifest.json --fill-assets-file ./out/fill_assets.json --driving-ranges-file ./out/driving_ranges.json --out-dir ./out/plan
   ```
   Dry-run and apply `batch_2_transitions_mutes.json` and `batch_3_music_sfx.json` in separate undo steps.
5. **Verify:** Run `audio_measure` across muted gaps, dialogue transitions, and ducked driving sections (`~ -22..-24 LUFS`, zero clipping), check visual seams with `frames`, and present `youtube_credits.txt`, `chapters.txt`, and `edit_report.md`.

---

## Mode B — Vertical Reels / Shorts / TikTok (`9:16`, `> 60 s`)

### Step 1: Discover Exact Mode B Schemas & Inspect Timeline
1. Call `toolbox` via `mcp` for `["set_project_setup","set_background","auto_reframe","import_subtitle_file","apply_text_preset","set_text_animation","apply_text_style_to_all","set_track","add_bookmark","detect_scenes","list_scenes","list_text_presets","list_text_animations"]` so every op conforms to the active Drift build.
2. Save `inspect_reels.json` (`inspect({"clips":true,"detail":true})`).

### Step 2: Analyze Full Gameplay & Find `>= 5` Highlights (`60–90 s`)
1. Run Drift `activity` (`--output ./out/activity.json`) and `detect_scenes` -> `list_scenes` (`--output ./out/scenes.json`).
2. Transcribe the full edited video with word timestamps:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py transcribe --audio "<edited_video_or_audio>" --full --model base --output ./out/full_transcript.json
   ```
3. Score and extract `>= 5` non-overlapping highlights (`60–90 s`) snapped to natural sentence/pause boundaries, along with internal `zoom_cues` and `sfx_cues`:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py find-highlights --transcript-file ./out/full_transcript.json --activity-file ./out/activity.json --scenes-file ./out/scenes.json --min-dur 60 --max-dur 90 --min-count 5 --output ./out/highlights.json
   ```

### Step 3: Set `1080×1920` Canvas, Slice Highlights & Apply Dynamic Motion/SFX
1. Dry-run then apply `set_project_setup` to `1080×1920` and set vertical 9:16 framing (`set_background` / blurred background duplicate + crisp centered gameplay foreground, or `detect_faces` + `auto_reframe` where appropriate).
2. Trim/split/place the selected `>= 5` highlight clips cleanly onto the timeline (with `add_bookmark` labeling each Reel `#1..#N`).
3. Search/install zero-quota `sfx` (`motion/whoosh`, `motion/impact`, `ui/pop`) and apply dynamic zoom keyframes (`set_keyframes` on `scaleX`/`scaleY` with `"smooth"`), subtle `zoom_pulse` / `camera_shake` / `camera_punch` at `zoom_cues`, dynamic transitions at internal sub-cuts, and synced SFX hits.
4. Re-inspect the arranged Reels timeline (`./out/inspect_reels_arranged.json`).

### Step 4: Dual EN/ES Viral Subtitles (`Subtitles EN` & `Subtitles ES`)
1. Generate English (original-language) viral SRT (`2–4` words/cue) + translation cue sheet matched to the arranged Reels timeline:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels_arranged.json --words-per-cue 3 --out-dir ./out/subs
   ```
2. Read `./out/subs/cues_for_translation.json` and translate every cue naturally into gamer Spanish (no external paid API), saving `./out/subs/translated_cues.json` (`[{"index": 1, "text_es": "..."}, ...]`).
3. Build synchronized dual SRTs:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels_arranged.json --translated-json ./out/subs/translated_cues.json --words-per-cue 3 --out-dir ./out/subs
   ```
4. Import `reels_en.srt` (`Subtitles EN`) and `reels_es.srt` (`Subtitles ES`) via `import_subtitle_file`. Apply bold animated text styling (`apply_text_preset` / `set_text` / `set_text_animation` + `apply_text_style_to_all`), positioned inside the safe vertical center-lower zone, and hide one track (`set_track({hidden:true})`) for easy single-toggle EN/ES export.
5. Verify visually with `capture` on representative frames from both tracks before finishing.

---

## Error Handling Summary

- **Drift connection/token error:** Stop immediately and ask the user for the live `URL` and `Token` from Drift; never guess ports or reuse old credentials.
- **Missing `uv` or `ffmpeg`:** Run `uv` setup or notify the user to install `ffmpeg`; offer Drift-native analysis fallback (`detect_silence`, `activity`, `frames`) when ML/ffmpeg cannot run.
- **`apply` error (`bad_args`, `not_found`, `conflict`):** Never ignore a failed `dry_run`. Read the error detail from `--output`, verify schemas with `toolbox`, refresh `inspect`, and regenerate the batch.
- **Model download timeout:** Retry via CLI backoff; if large Whisper model fails, rerun with `--model base` or `--model tiny`.
