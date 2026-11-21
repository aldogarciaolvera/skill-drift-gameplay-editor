---
name: drift-gameplay-editor
description: >-
  Senior AI gameplay editor & viral clipper for CutWire Drift MCP (and
  transcript-first pre-edit planning). Handles two modes: (1) Long YouTube
  16:9 gameplay (technical breakdown, optional 0:00-0:15 cold open teaser,
  pause/dead-screen cuts and jump-cut pacing, copyright-radio muting
  preserving dialogue, Free-To-Use music fill + driving ducking, bookmarks, and
  SEO/CTR YouTube chapters + credits), and (2) Vertical 9:16 Reels/TikTok/Shorts
  (60-90s narrative highlights by default or 30-60s micro-clips, Hook -> Setup
  -> Climax -> Payoff, clean 9:16 gameplay/facecam framing, subtle Reel-entry
  accent only, strict 9:16 Safe Zones, and native EN/ES karaoke subtitles with
  natural gamer slang).
---

# CutWire Drift Gameplay Editor (`drift-gameplay-editor`)

Acts as a Senior Audiovisual Editor automating **editorial-first** gameplay post-production in **CutWire Drift** via `/mcp` combined with local `ffmpeg`, `YAMNet`, and `faster-whisper` analysis (and clean transcript-based editorial roadmaps when no live Drift session is connected yet).

Always read [references/drift-workflows.md](./references/drift-workflows.md) for verified Drift MCP tool patterns, framing modes, 9:16 Safe Zones (`20%` bottom / `15%` right), LUFS loudness targets, GTA 6 / copyright compliance rules, and batch rules before building `apply` batches.

> **Editorial restraint rule:** Focus on clean storytelling, tight pacing, and crisp audio. Avoid flashy/excessive effects (`camera_shake`, repeated `zoom_pulse`, mid-clip popups, or impact spam). In Mode B Reels, apply **at most a subtle entry accent at the start of each Reel** (`0.0–0.85 s`).

---

## Prerequisites & Intake

1. **`uv` (Required Python runner):** Check `uv --version`. If missing, follow the **`uv` skill** setup steps to install `uv` and add it to `PATH`.
   - Always execute the helper CLI directly with `uv run "<skill-dir>/scripts/drift_gameplay_cli.py" ...` (never `uv run python ...`, which bypasses the script's PEP 723 dependency metadata).
2. **`ffmpeg` & `ffprobe`:** Verify `ffmpeg -version` and `ffprobe -version` are on `PATH` before scanning media. Always decode audio via `ffmpeg` to 16 kHz PCM/WAV before Whisper (avoids PyAV metadata errors on long MP4/MP3 files). If unavailable locally, inform the user and fall back to Drift's built-in `detect_silence`, `activity`, `frames`, and `audio_presence` tools where supported.
3. **Active Drift MCP Session (or Transcript-Only Fallback):**
   - When editing in Drift, ask the user for (or verify from `DRIFT_MCP_URL` / `DRIFT_MCP_TOKEN`) the active `URL` and Bearer `Token`, then verify connectivity:
     ```powershell
     uv run ./scripts/drift_gameplay_cli.py mcp --url "<URL>" --token "<TOKEN>" --tool inspect --args-json '{"clips":true,"detail":true}' --output ./out/inspect_initial.json
     ```
   - **If the user only provides a raw video without timestamps or a transcript without an open Drift session:** either run `transcribe --full` locally first, or request approximate highlights/timestamps if media cannot be read, and output a complete **Editorial Roadmap & Clip Matrix** ready to execute in Drift once connected.
4. **Ficha Técnica (Mandatory Pre-Edit Diagnosis):** Before cutting, identify and record: **Game**, **Total Duration**, **Creator Tone** (humorous / competitive / narrative / horror), and **Key Narrative Milestones** (opening hook, rising tension, climax/payoff).

---

## Copyright, Monetization & GTA 6 Rules

1. **In-game radio & licensed music:** Never publish unmuted copyrighted radio songs (Content ID / Twitch VOD mutes); mute non-dialogue stretches cleanly or turn radio audio off when capturing.
2. **First-7-seconds & hook safety (YouTube / Reels):** Avoid graphic blood/gore or shock violence in the opening `7 s` or thumbnails, and do not extract meaningless NPC-killing compilations; hook with situation, dialogue, surprise, or setup instead. Start directly on action/speech with zero dead air or slow black fades.
3. **Spoilers & creator permissions:** Do not isolate ending spoilers or raw unedited cutscenes without narrative context. When editing another streamer's VOD (only with permission), include visible on-screen/caption credit (`Clip: <channel>`).

---

## Mode Selector

Inspect `inspect_initial.json` and confirm the workflow mode with the user (or follow the user's explicit request):

- **Mode A — Long Gameplay (YouTube 16:9):** Multi-clip or raw long gameplay needing pause/loading/dead-screen cuts, copyright music detection & muting (preserving dialogue), Free To Use music replacement + driving auto-ducking, clean transitions, bookmarks, and YouTube credits/chapters.
- **Mode B — Vertical Reels / Shorts / TikTok (`9:16`, `> 60 s`):** Designed for a project loaded with gameplay (already edited or raw stream/capture). Converts canvas to `1080×1920`, extracts `>= 5` self-contained narrative highlights (`60–90 s`) with peak near `~65–70 %` of the clip, applies clean 9:16 framing (`solo_juego` / `facecam` / `a_camara`) + **subtle Reel-entry accent only**, normalizes near `−14 LUFS`, and builds dual `Subtitles EN` + `Subtitles ES` tracks ready for toggling and export.

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
   uv run ./scripts/drift_gameplay_cli.py transcribe --audio ./out/audio_v1.f32 --windows-file ./out/music_v1.json --model small --device auto --output ./out/transcript_v1.json
   ```
   - On CUDA GPUs, `--model large-v3-turbo` improves proper-noun accuracy; if model download/VRAM fails, rerun with `--model base --device cpu`.
3. Inspect ambiguous song edges or instrumental segments (`frames` / `capture` to check in-game radio/cutscene context). Never mute confirmed dialogue or safe original game score; queue uncertain cases for `add_bookmark` and `edit_report.md`.

### Step 4: Source Free To Use Music & Subtle SFX (Zero Quota)
1. Check `list_assets` in `inspect` first to reuse any tracks/SFX already in the bin.
2. Search/install **only** from `stock.music` (`provider: "freetouse"`, `quota: null`) and `sfx` (`quota: null`, e.g. subtle `motion/whoosh` only for real time-jump cuts). **Never** spend `stock.audio` or `stock.video` quotas.
3. Call `market_item` for each selected track to obtain exact `license.attribution` (`Title by Artist (freetouse.com)`) and `license.url`, install with `market_install({"source":"stock.music","id":...,"wait":120})`, and save track metadata (`id`, `title`, `artist`, `attribution`, `license_url`, `dur`, `volume`, `mood`) into `fill_assets.json`. Target `~ -22..-24 LUFS` for background music under `~ -14 LUFS` dialogue/gameplay.

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
4. **Stage 2 (`post-cuts`):** Generate subtle time-jump transitions (`crossfade`/`dip`, `0.25–0.35 s`), monotonic `set_keyframes` volume mutes (`0.15 s` linear ramps), `Audio 3` Free To Use fills (both muted gaps and driving-without-radio ranges with `duck_under`), `Audio 4` subtle time-jump `whoosh` SFX, review `add_bookmark` markers (including optional `0:00–0:15` Cold Open teaser hook and jump-cut notes for slow pacing stretches), plus `youtube_credits.txt`, SEO/CTR-oriented `chapters.txt` (`[HH:MM:SS]` / `MM:SS`), and `edit_report.md`:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py plan-youtube --stage post-cuts --inspect-file ./out/inspect_post_cuts.json --pauses-file ./out/pauses_manifest.json --music-file ./out/music_manifest.json --transcript-file ./out/transcript_manifest.json --fill-assets-file ./out/fill_assets.json --driving-ranges-file ./out/driving_ranges.json --out-dir ./out/plan
   ```
   Dry-run and apply `batch_2_transitions_mutes.json` and `batch_3_music_sfx.json` in separate undo steps.
5. **Verify:** Run `audio_measure` across muted gaps, dialogue transitions, and ducked driving sections (`~ -14 LUFS` master / `~ -22..-24 LUFS` BGM, zero clipping), replace generic chapter labels in `chapters.txt` with high-CTR event titles, check visual seams with `frames`, and present `youtube_credits.txt`, `chapters.txt`, and `edit_report.md`.

---

## Mode B — Vertical Reels / Shorts / TikTok (`9:16`, `60–90 s` or `30–60 s`)

> **Length profiles:** Default to **`>= 5` clips of `60–90 s`** for monetizable long Reels/Shorts (`> 60 s`). When the user explicitly requests snappy TikTok/short clips, switch to **`3–5` micro-clips of `30–60 s`** (`--min-dur 30 --max-dur 60 --min-count 3 --max-count 5`).

### Step 1: Discover Exact Mode B Schemas & Inspect Timeline
1. Call `toolbox` via `mcp` for `["set_project_setup","set_background","auto_reframe","detect_faces","import_subtitle_file","set_subtitle_cues","generate_subtitles","apply_text_preset","set_text","apply_text_style_to_all","set_track","add_bookmark","detect_scenes","list_scenes","list_text_presets","list_text_animations","normalize_volume","audio_measure"]` so every op conforms to the active Drift build.
2. Save `inspect_reels.json` (`inspect({"clips":true,"detail":true})`).

### Step 2: Analyze Full Gameplay & Curate Narrative Highlights
1. Run Drift `activity` (`--output ./out/activity.json`) and `detect_scenes` -> `list_scenes` (`--output ./out/scenes.json`).
2. Extract 16 kHz PCM (`ffmpeg` / `.f32`) and transcribe the full gameplay with word timestamps (prefer `--model small` on CPU or `--model large-v3-turbo` on CUDA so proper nouns stay accurate):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py transcribe --audio "<edited_video_or_f32>" --full --model small --device auto --vad-filter --output ./out/full_transcript.json
   ```
3. Score and extract candidate highlights (placing climax/reaction peaks around `~65–70 %` of the clip using robust Median/MAD audio energy z-scores + speech/hype + scenes):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py find-highlights --transcript-file ./out/full_transcript.json --audio-f32 ./out/audio_full.f32 --activity-file ./out/activity.json --scenes-file ./out/scenes.json --min-dur 60 --max-dur 90 --min-count 5 --max-count 10 --output ./out/highlights.json
   ```
4. **Mandatory editorial review (`highlights.md`):** Read the generated `./out/highlights.md` transcript previews completely. Never pick by numeric score alone:
   - Structure every clip around a clean arc: **Scroll-stopping Hook (`0–3 s`) -> Setup -> Rising Tension/Climax -> Payoff or Cliffhanger**, with exact `[HH:MM:SS] - [HH:MM:SS]` In/Out bounds.
   - Discard silent stretches, repetitive background radio/TV ads, or clips lacking a clear setup and punchline/payoff.
   - Snap head/tail boundaries cleanly to complete sentences (`start` on an immediate hook, `end` right after the reaction) while keeping every Reel inside the chosen duration target and confirming zero graphic blood/violence in the first `7 s`. If ambiguous, present top 5–8 candidates to the user before cutting.

### Step 3: Clean `1080×1920` Framing, Cuts & Subtle Entry Accent Only
1. Dry-run then apply `set_project_setup` to `1080×1920` and inspect `capture` / `detect_faces` to choose the right 9:16 layout:
   - **`solo_juego` (default Blurred Full-Frame Stack for clean gameplay):** Centered 16:9 foreground (`scale ~1.15–1.20`) over blurred duplicate background (`builtin.effects.gaussian_blur`, `opacity ~0.72`, **`volume: 0`**). If cutscenes contain baked 21:9 letterbox bars, scale background to `~4.5` so no black bars repeat top/bottom.
   - **`facecam` (streamer cam in corner):** Top 1/3 facecam (`1080×640`) + bottom 2/3 gameplay (`1080×1280` cropped away from the camera corner so the facecam never appears twice).
   - **`a_camara` (full-screen reaction/dialogue):** Vertical 9:16 crop centered on the speaker (`auto_reframe`).
2. Trim/split/place the selected highlight clips cleanly onto the timeline (`2.0 s` separator gaps optional, `add_bookmark` labeling each Reel `#1..#N`), with short `equalPower` audio/video micro-fades (`in: 0.10–0.15 s, out: 0.15–0.20 s`) to eliminate clicks.
3. **Subtle entry-only polish (NO mid-clip effect spam):**
   - Apply **no** mid-clip `camera_shake`, **no** repeated `zoom_pulse`, and **no** mid-dialogue `Punch`/`impact` hits.
   - Apply **only** a clean entry accent over the first `0.0–0.85 s` of each Reel (`set_keyframes` `smooth` gentle scale settle e.g. `base * 1.06 -> base` over `0.85 s`, plus optional soft zero-quota `whoosh` at `tl_start` at `volume ~0.35–0.40` with `equalPower` fades). Between internal sub-cuts (if any), use clean cuts or brief `0.20–0.25 s` `crossfade`.
4. Normalize foreground dialogue/gameplay near `−14 LUFS` (`TP <= −1.5 dBTP`, verified via `audio_measure`), then save `./out/inspect_reels_arranged.json`.

### Step 4: Dual EN/ES Native Karaoke Subtitles & Strict 9:16 Safe Zones (`Subtitles ES` & `Subtitles EN`)
> **Strict Safe Zones (`1080×1920`):** Keep every caption inside the safe central band — strictly above the **bottom 20 %** UI zone (`y < 1536`, default `y = 1435`), clear of the **right 15 %** like/comment button column (`x < 918`, keep lines short via `--max-chars-per-cue 24` centered at `x = 540`), below top headers (`y > 230`), and away from any burned-in game subtitles.
1. Generate original-language cues (`1–3` words / `<= 24` chars, breaking cleanly on punctuation/pauses) + translation cue sheet matched to the arranged Reels timeline:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels_arranged.json --words-per-cue 3 --max-chars-per-cue 24 --out-dir ./out/subs
   ```
   *(Optional `--corrected-script ./out/clean_scripts.json` realigns cleaned/edited transcripts back onto Whisper's exact word timestamps before cue grouping.)*
2. Read `./out/subs/cues_for_translation.json`, fix any Whisper proper-noun typos (via `"text_en"` if needed), and translate every cue into **natural gamer Spanish** (no external paid API; keep authentic gaming terms like `clutch`, `headshot`, `rushear`, `nerfeo`, `loot`, `spot` natural instead of stiff literal translations), saving `./out/subs/translated_cues.json` (`[{"index": 1, "text_en": "...", "text_es": "..."}, ...]`).
3. Emit synchronized `reel_clips_en.json`, `reel_clips_es.json`, and `reels_*.srt`:
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels_arranged.json --translated-json ./out/subs/translated_cues.json --words-per-cue 3 --max-chars-per-cue 24 --out-dir ./out/subs
   ```
4. Push **independent per-Reel subtitle clips** onto two separate tracks in Drift (`Subtitles ES` visible on Track 0, `Subtitles EN` hidden on Track 1, styled with `karaoke-pop` + `Anton` 64 px uppercase yellow word-by-word highlight at `y=1435`):
   ```powershell
   uv run ./scripts/drift_gameplay_cli.py apply-reel-subs --url "<URL>" --token "<TOKEN>" --manifest ./out/subs/reel_subs_manifest.json --primary-lang es --output ./out/subs/apply_subs_result.json
   ```
5. Verify visually with `capture` on active speech timestamps from **both** tracks (toggling `set_track` hidden flags during check, then restoring preferred visible track) and confirm zero Safe Zone collisions before saving.

---

## Final Deliverable Structure

Present results clearly using this editorial structure alongside all generated files/timeline state:
1. **Ficha Técnica del Metraje:** Game, total duration, creator tone, and major narrative milestones.
2. **Hoja de Ruta para Video Largo (YouTube 16:9, if Mode A or planning):** Optional `0:00–0:15` Cold Open hook, pause/jump-cut summary, audio/BGM mix notes, and SEO/CTR chapter table (`[HH:MM:SS]`).
3. **Matriz de Clips Verticales (Reels/TikTok/Shorts 9:16, if Mode B or planning):** For each Clip `#1..#N`: title, exact `In/Out` (`[HH:MM:SS] - [HH:MM:SS]`), narrative reason (`Hook 0–3 s`, `Setup -> Climax -> Payoff`), framing layout (`solo_juego` / `facecam` / `a_camara`), and bilingual ES/EN subtitle files (`reels_es.srt`, `reels_en.srt`, plus `reel_subs_manifest.json` / Drift track toggles).

---

## Error Handling Summary

- **Drift connection/token error:** Stop immediately and ask the user for the live `URL` and `Token` from Drift; never guess ports or reuse old credentials. If the user only needs a transcript-based editorial plan first, proceed with the **Transcript-Only Fallback** in Prerequisites.
- **Missing `uv` or `ffmpeg`:** Run `uv` setup or notify the user to install `ffmpeg`; offer Drift-native analysis fallback (`detect_silence`, `activity`, `frames`) when ML/ffmpeg cannot run.
- **`apply` error (`bad_args`, `not_found`, `conflict`):** Never ignore a failed `dry_run`. Read the error detail from `--output`, verify schemas with `toolbox`, refresh `inspect`, and regenerate the batch.
- **Model download / CUDA error:** Retry via CLI backoff or auto-fallback to `--device cpu`; if a larger Whisper model fails, rerun with `--model small` or `--model base`.
