# CutWire Drift MCP Reference & Editing Workflows

Verified tool conventions, schemas, presets, and audio/video recipes for automating gameplay editing in CutWire Drift.

---

## 1. Drift MCP Core Protocol

- **Endpoint:** `POST <DRIFT_URL>` (e.g. `http://127.0.0.1:<port>/mcp`) with header `Authorization: Bearer <TOKEN>` and JSON-RPC 2.0:
  ```json
  {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": { "name": "<tool>", "arguments": { ... } }
  }
  ```
- **Session discovery:** Always request the current session's `URL` and `Token` from the user (or read `DRIFT_MCP_URL` / `DRIFT_MCP_TOKEN` environment variables). Never hardcode or reuse stale values from older sessions.
- **Inspection:**
  - `inspect({"clips": true})` — fast timeline, track, asset, selection, and undo snapshot.
  - `inspect({"clips": true, "detail": true})` — adds effect stacks, fades, speed, subtitle cues, face-track/stabilize fields, and active background jobs.
  - `inspect({"clips": true, "cues": true})` — adds `subtitleCues` without full detail payload.
- **Schema discovery before unfamiliar ops:**
  Run `toolbox({"ops": [...]})` or `toolbox({"name": "<toolbox>"})` via `drift_gameplay_cli.py mcp` whenever calling an op whose exact parameter schema has not been verified in the current Drift build (especially Mode B ops like `set_project_setup`, `set_background`, `auto_reframe`, `import_subtitle_file`, `apply_text_preset`, `set_text_animation`, `apply_text_style_to_all`, `set_track`, `add_bookmark`, `detect_scenes`, `list_scenes`).
- **Batching & Undo Hygiene:**
  - `apply({"ops": [...], "dry_run": true})` validates every op before touching the project. **Always run `dry_run: true` first.**
  - `apply({"ops": [...]})` executes all ops in a single undo step (`Ctrl+Z`).
  - `apply({"ops": [...], "atomic": true})` rolls back automatically if a project-internal op fails.
  - Keep `apply` batches grouped by editing phase (cuts -> transitions & mutes -> music & SFX -> subtitles & motion) so the user can undo cleanly phase by phase.
  - Always address clips by **UUID** (`clip`), not positional `track` + `index`, because `add_track` and splits shift indices.
  - Use `geometry` and `removed_clips` returned by `apply` (or re-run `inspect({"clips": true})`) before subsequent edits that depend on split/inserted clip IDs.
  - Selection-based ops (`separate_audio`, `unlink_audio`, `link_clips`, `merge_clips`) take no `clip` argument — call `select_clips({"clips": [...]})` first.

---

## 2. Mode A — Long YouTube Gameplay (16:9) Recipes

### 2.1 Separating Linked Audio

Before muting music segments independently of video cuts:

1. Check `inspect({"clips": true})`. If video clips still carry embedded audio (no matching separated `audio` clips linked via `linkId`), run:
   - `select_clips({"clips": ["<video-uuid-1>", ...]})`
   - `separate_audio({})`
2. Re-run `inspect({"clips": true})` to map each video clip's `linkId` to its separated audio clip UUID.

### 2.2 Timeline Coordinate Mapping

Given a Drift clip object (`start`, `inPoint`, `outPoint`, `speed`, default `speed = 1`):

- Source second `t_src` (inside `[inPoint, outPoint]`) -> Timeline second:
  `t_tl = start + (t_src - inPoint) / speed`
- Timeline second `t_tl` -> Source second:
  `t_src = inPoint + (t_tl - start) * speed`

### 2.3 Pause, Loading, Map & Dead-Screen Cuts

- **Head/Tail trims:** Use `set_trim({"clip": "<uuid>", "in": <src_in>, "out": <src_out>, "ripple": true})`. `ripple: true` trims linked audio too and shifts later clips left cleanly.
- **Interior cuts:** Split video clips from **latest timeline time to earliest** using `split_clip({"clip": "<uuid>", "at": <tl_time>})` so earlier split points stay inside the original UUID. Then delete the unwanted middle segments via `delete_clips({"clips": [...], "ripple": true, "with_effects": true})`.
- **Visual verification:** Use `frames({"at": [...]})` or `capture({"at": ...})` around candidate cuts before deleting.
- **Conservative thresholds:**
  - Pause/menu/dead screens: cut if `>= 2.0 s` (confirmed visually).
  - Map screens: cut only if `>= 5.0 s`; shorter map checks should either be kept or bookmarked.
  - Static loading/desktop screens: cut if `>= 3.0 s` after verifying it is not an intentional cutscene/dialogue moment.

### 2.4 Transitions & Subtle SFX on Time-Jump Cuts

- Apply on adjacent clips where a genuine time jump occurred (using post-cut `clip` UUID of the **outgoing** clip):
  ```json
  {
    "tool": "add_transition",
    "args": {
      "clip": "<outgoing-video-uuid>",
      "kind": "crossfade",
      "duration": 0.3,
      "linked_audio": true
    }
  }
  ```
- Recommended Mode A transition `kind` values:
  - `crossfade` (`0.25–0.35 s`) — clean time jump during active gameplay.
  - `dip` (`0.30–0.40 s`, Dip to Black) — major scene skip, loading screen removal, or chapter break.
- Pair time-jump transitions with a subtle `whoosh` from Drift `sfx` (`quota: null`, e.g. category `motion/whoosh`) placed on an `Audio 4` SFX lane at `cut_time - 0.12 s` at `volume: 0.35–0.45` with `set_fade` (`in: 0.03, out: 0.08`).

### 2.5 Copyright Music Muting (Preserving Dialogue)

- Use `set_keyframes` on the separated audio clip's `volume` property:
  ```json
  {
    "tool": "set_keyframes",
    "args": {
      "clip": "<audio-uuid>",
      "interpolation": "linear",
      "replace": true,
      "keys": {
        "volume": [
          [0.0, 1.0],
          [43.35, 1.0],
          [43.5, 0.0],
          [48.2, 0.0],
          [48.35, 1.0]
        ]
      }
    }
  }
  ```
- **Invariants:**
  - Every keyframe time `t` MUST be strictly monotonic (`t_i > t_{i-1}`) and inside `[clip.start, clip.start + clip.duration]`.
  - Use `0.15 s` linear ramps (`a - 0.15 -> a`, `b -> b + 0.15`) to avoid clicks.
  - Never mute speech/dialogue or confirmed safe game score. For uncertain instrumental tracks, leave unmuted, place `add_bookmark`, and document in `edit_report.md`.

### 2.6 Free To Use Music Fill & Driving Auto-Ducking

- **Marketplace rules:**
  - Use **only** `stock.music` (`provider: "freetouse"`, `quota: null`) and `sfx` (`quota: null`).
  - **Avoid** `stock.audio` and `stock.video` (daily quota of `1–3/day`).
- **Verified Free To Use reference tracks (available via `stock.music`):**
  | Title | Artist | Catalog ID | Duration | Suggested Mood | Typical Volume (`~ -22..-24 LUFS`) |
  |---|---|---|---|---|---|
  | `Night Drive` | Walen | `ZnJlZXRvdXNlOm11c2ljOjQ3MmZjOTk1LWQzZDEtMDQzNC1hZWRlLWI1NzBhZjM3N2E4Zg` | `137.507 s` | Night driving / synth chill | `0.32` |
  | `Road Trip` | HiLau | `ZnJlZXRvdXNlOm11c2ljOjEyNWUyNTc5LWRmZDEtNDk4Yy1iYjEyLWUwMmRkM2MwY2ZlNA` | `188.917 s` | Daytime driving / upbeat | `0.26` |
  | `Mellow Sky` | Zambolino | `ZnJlZXRvdXNlOm11c2ljOmNjYzdlZGZjLTMwNzUtNDBkZi05OGIxLTZjNjM4MjY2NWMzOA` | `157.060 s` | Interiors / relaxed / chill | `0.34` |
  | `Night Hawk` | HiLau | `ZnJlZXRvdXNlOm11c2ljOmI2MWM3YmE3LWJhMTQtNGVlNC1iMGRjLTJlMmU1YTMyNjU0Yw` | `188.865 s` | Car radio / night cruise | `0.26` |
- Always run `market_item({"source": "stock.music", "id": "<id>"})` when adding new tracks to collect exact `license.attribution` and `license.url` for `youtube_credits.txt`, and check loudness via `audio_measure` / `normalize_volume({"clip": ..., "target_lufs": -23})`.
- **Placement rules (`Audio 3`):**
  - Place first clip with `place_clip({"asset": "<id>", "track": 3, "at": <t>, "new_track": true})` (or on an existing `Audio 3` track), then `insert_clip({"asset": ..., "track": <idx>, "at": <t>, "in": <s_in>, "out": <s_out>, "ripple": false})` for subsequent gaps.
  - Advance `s_in` continuously across short dialogue interruptions within the same scene so the track flows instead of restarting from `0.0 s`.
  - Apply `set_fade({"clip": "<id>", "in": 0.25, "out": 0.25})` and `set_fade_curve({"clip": "<id>", "curve": "equalPower", "edge": "both"})`.
  - For **driving segments without radio** that include dialogue, place the Free To Use track continuously across the driving span and run:
    ```json
    {
      "tool": "duck_under",
      "args": {
        "clip": "<music-clip-uuid>",
        "over_track": 1,
        "amount": 0.25,
        "attack": 0.12,
        "release": 0.25
      }
    }
    ```
    (Pass `over_clips: [...]` if dialogue spans multiple audio lanes.)

### 2.7 Cold Open Teaser, Jump-Cut Pacing & SEO/CTR Chapters

- **Optional Cold Open (`0:00–0:15`):** Place a short teaser of the session's highest-tension non-spoiler moment right at `0:00` (avoiding graphic blood/violence in the opening `7 s`), followed by a brief `dip`/`crossfade` into the main match.
- **Jump-Cut Pacing on Slow Stretches:** When footage has low-action traversal or quiet exploration without strong highlights, mark/cut dead pauses with clean jump cuts (keeping dialogue intact) rather than adding flashy effects.
- **SEO & CTR Chapters (`chapters.txt`):** Replace generic `"Part N"` titles with concise, clickable event-based titles (`MM:SS` or `H:MM:SS`) reflecting real game missions, fights, or twists from the transcript.

---

## 3. Mode B — Vertical Reels / Shorts / TikTok (`9:16`, Canonical `ClipsParte2` Preset)

> **Always check schemas first:** Run `toolbox` on `project`, `subtitles`, `text`, `scene`, `effects`, and `auto_reframe` / `detect_faces` / `set_track` / `add_bookmark` on the active Drift build before building `apply` batches.
> **Long footage `activity` rule:** Drift enforces `end - start <= 3600` and `peaks <= 30`. Split `activity` calls into `<= 3600 s` spans on videos over 1 hour and merge them before running `find-highlights`.

### 3.1 Canvas Setup & Canonical `solo_juego` 9:16 Stack (`1080×1920`)

1. Switch canvas to `1080×1920 @ 30 fps` via `set_project_setup` and `set_background({kind: "color", color: "#ff0b0b12"})`, then inspect representative frames with `frames` / `capture` + `detect_faces` before choosing the vertical framing mode:
   - **`solo_juego` — Blurred Full-Frame Stack (canonical `ClipsParte2` default for pure gameplay / cutscenes):**
     - Background video lane (`Video 1`): **`scaleX: 4.5, scaleY: 4.5`** (eliminates baked 21:9 letterbox bars across both gameplay and cutscenes uniformly) + `builtin.effects.gaussian_blur` (**`u_blurRadius: 42`**), **`opacity: 0.72`**, **`volume: 0.0`**, fades `in: 0.12, out: 0.15` (`equalPower`) over `#ff0b0b12`.
     - Foreground gameplay lane (`Video 2`, above background): center-fit (**`scaleX: 1.18, scaleY: 1.18`**, `positionX: 540, positionY: 960`, `opacity: 1.0`, fades `in: 0.12, out: 0.15` `equalPower`), leaving crisp blurred headroom and lower caption space.
   - **`facecam` — Streamer Cam + Gameplay Split (when `detect_faces` / `frames` shows a fixed corner camera):**
     - Crop/position the corner facecam cleanly across the upper third (`1080×640`) and the gameplay across the lower two-thirds (`1080×1280`), horizontally offset away from the facecam corner so the camera never duplicates inside the gameplay box. Save the camera crop layout per streamer channel.
   - **`a_camara` — Speaker Vertical Crop (talking-head / reaction full frame):**
     - Use `detect_faces` + `auto_reframe` (`crop` to `9:16` following the face).

### 3.2 Highlight Extraction (`5` Curated Clips `60–90 s`, or `3–5` Micro-Clips `30–60 s`)

1. Run Drift `activity` (`<= 3600 s` chunks, `peaks: 30`) and `detect_scenes` (`list_scenes` for shot boundaries/scores).
2. Run `drift_gameplay_cli.py transcribe --full --model small --device auto --vad-filter` (with 16 kHz `.f32`/PCM audio via `ffmpeg`) to capture accurate word-level timestamps across the full source.
3. Run `drift_gameplay_cli.py find-highlights --audio-f32 ./out/audio_full.f32 --min-dur 60 --max-dur 90 --min-count 5 --max-count 8` (or `--min-dur 30 --max-dur 60 --min-count 3 --max-count 5` when the user explicitly requests short TikTok micro-clips), combining robust Median/MAD audio energy z-scores, transcript hype/laughter/gaming cues, `activity`, and `scenes`, placing peaks near `~65–70 %` of each window.
4. **Curate top `5` via `highlights.md`:** Read every candidate's full preview before cutting. Pick the strongest **`5`** clips with exact `[HH:MM:SS] - [HH:MM:SS]` In/Out boundaries confirming: **Hook (`0–3 s`) -> Setup -> Rising Tension / Climax -> Payoff or Cliffhanger**. Drop silent stretches, repetitive ambient/radio lines, unmuted copyright songs, and avoid graphic violence/blood in the first `7 s`.
5. Place the curated `5` highlights separated by **`2.0 s` gaps** on the timeline with one `add_bookmark` per Reel at `tl_start` (`Reel #N (XXs) - <Short Title>`).

### 3.3 Clean Editing Discipline, Subtle Reel-Entry Accent & `utility.limiter` Audio

> **Editorial Focus:** Keep the body of every Reel distraction-free. Do **not** spam `camera_shake`, `camera_punch`, mid-clip `zoom_pulse`, or impact hits across dialogue.

- **Boundary micro-fades:** Apply `set_fade({in: 0.12, out: 0.15})` and `set_fade_curve({curve: "equalPower", edge: "both"})` on foreground and background Reel clips.
- **Entry accent only (`0.0–0.85 s` of each Reel):**
  - Gentle `set_keyframes` settle (`interpolation: "smooth"`, `replace: true`) on foreground `scaleX` / `scaleY`: `[tl_start, 1.25] -> [tl_start + 0.85, 1.18]` (for `solo_juego` base `1.18`, or `base * 1.06 -> base`), with no further zoom keyframes later in the Reel.
  - Zero-quota **`01_woosh.flac`** (`source: "sfx"`, catalog id `aa47d9f251567c87`, trimmed **`in: 0.35, out: 0.95` = `0.60 s`**) placed once at each Reel's `tl_start` on bottom `Audio 1` (`volume: 0.35`, `set_fade` `in: 0.02, out: 0.15`, `equalPower`).
- **Internal sub-cuts (only if a Reel stitches two moments):** Clean cut or brief `crossfade` (`0.20–0.25 s`, no flashy swirl/shake effects).
- **Audio loudness & peak protection (`utility.limiter` + `audio_measure`):** Keep foreground `volume: 1.0` (do **not** raise post-limiter clip `volume > 1.0`) and boost via `add_audio_effect({effect: "utility.limiter"})` setting `ceiling: 0.81..0.84` (`<= -1.4 dBTP`) and `drive` (`1.4..2.3` per Reel depending on dialogue level). Measure every Reel individually with `audio_measure`, targeting **`-14 .. -16.5 LUFS`**, **`TP <= -1.25 dBTP`**, and `0` clipping. Save `./out/inspect_reels_arranged.json`.

### 3.4 Dual EN/ES Native Karaoke Subtitles & Strict 9:16 Safe Zones (`apply-reel-subs` Option C)

> **Strict 9:16 Safe Zone Box (`1080×1920`):**
>
> - **Top `12 %` (`y <= 230`):** Reserved for search bar / Follow / platform header.
> - **Bottom `20 %` (`y >= 1536`):** Reserved for TikTok/Reels captions, audio ticker, and handle.
> - **Right `15 %` (`x >= 918`):** Reserved for Like, Comment, Save, and Share icons — enforce short line lengths (`--max-chars-per-cue 24`) centered at `x = 540` so lines never approach `x = 918`.
> - **Recommended subtitle anchor:** `positionX: 540`, `positionY: 1435` (`230 <= y <= 1520`), cleanly below the centered 16:9 gameplay box (and below any burned-in game subtitles) while staying clear of all HUD overlays.

1. Run `build-reel-subs` (first pass without `--translated-json`) to generate `cues_for_translation.json`, `reel_clips_en.json`, and `reels_en.srt` (`1–3` words and `<= 24` chars per cue, breaking cleanly on `[.,;:!?…]` or pauses `> 0.35 s`, matched per Reel via `--inspect-file`). Pass `--corrected-script` to align cleaned transcript lines onto Whisper word timings if needed.
2. Read `cues_for_translation.json`, fix any Whisper misspellings (optional `"text_en"` cleanup), and translate into **natural gamer Spanish** (`translated_cues.json`, preserving cue indices and keeping natural gaming slang such as `clutch`, `headshot`, `rushear`, `nerfeo`, `loot`, `spot`).
3. Run `build-reel-subs` (second pass with `--translated-json translated_cues.json`) to emit synchronized `reel_clips_en.json`, `reel_clips_es.json`, `reels_en.srt`, `reels_es.srt`, and `reel_subs_manifest.json`.
4. Run `drift_gameplay_cli.py apply-reel-subs --manifest ./out/subs/reel_subs_manifest.json --primary-lang es --output ./out/subs/apply_subs_result.json`, which executes Drift's native Option C pipeline cleanly with `dry_run: true` before every `atomic: true` batch:
   - Staging-creates **one independent subtitle clip per Reel** via `add_subtitle_clip` (`staging >= dur + 30`), trims each clip to its Reel duration (`set_trim({in: 0, out: reel_dur})`), moves it into exact timeline position (`move_clip({at: reel_start})`), and populates only that Reel's cues converted to **clip-local `[0..reel_dur]` seconds** (`set_subtitle_cues`, required by Drift's player even when `clip.start > 0`).
   - Prepends a second subtitle track (`add_track({type: "subtitle"})`) so `ES` (Track 0, `hidden: false`) and `EN` (Track 1, `hidden: true`) stay completely isolated without colliding.
   - Applies Drift's verified word-by-word yellow karaoke style (`apply_text_preset("karaoke-pop")` + `set_text`: `fontFamily: "Anton"`, `pixelSize: 64`, `letterSpacing: 2`, `textCase: "upper"`, `align: "center"`, `valign: "center"`, `boxEnabled: false`, `accent: {rule: "karaoke", color: "#ffffd400", colorEnabled: true, sizeScale: 1.0}`) and centers in the safe zone (`set_transform({positionX: 540, positionY: 1435})`).
5. Visually verify both tracks via `capture` (checking multiple cue midpoints across Reels to confirm yellow word-by-word highlighting tracks live with zero right-margin or bottom-20% UI overlap).
