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

---

## 3. Mode B — Vertical Reels / Shorts / TikTok (`9:16`, `> 60 s`) Recipes

> **Always check schemas first:** Run `toolbox` on `project`, `subtitles`, `text`, `scene`, and `auto_reframe` / `detect_faces` / `set_track` / `add_bookmark` on the active Drift build before building `apply` batches.

### 3.1 Canvas Setup & Vertical 9:16 Framing (`1080×1920`)
1. Switch canvas to `1080×1920` via `set_project_setup` and inspect representative frames with `capture` + `detect_faces` before choosing the vertical framing mode:
   - **`solo_juego` — Blurred Full-Frame Stack (default for pure gameplay / cutscenes):**
     - Background video lane: scale to fill full `1920 px` vertical height (`scale 1.78` for clean 16:9, or **`scale ~4.5`** if 21:9 cutscenes contain baked black letterbox bars so no dark bars repeat top/bottom) + `builtin.effects.gaussian_blur` (`u_blurRadius: ~40`), `opacity: ~0.72`, **`volume: 0.0`** over dark background (`set_background({kind: "color", color: "#ff0b0b12"})`).
     - Foreground gameplay lane (above background): center-fit (`scaleX: 1.15..1.20, scaleY: 1.15..1.20`, `positionX: 540, positionY: 960`). On `1080×1920`, a `1920×1080` clip at `scale: 1.0` renders at `1080×607.5 px` (fit-width), so `scale: 1.18` fills `~717 px` centered vertically, leaving crisp blurred headroom and lower caption space.
   - **`facecam` — Streamer Cam + Gameplay Split (when `detect_faces` / `frames` shows a fixed corner camera):**
     - Crop/position the corner facecam cleanly across the upper third (`1080×640`) and the gameplay across the lower two-thirds (`1080×1280`), horizontally offset away from the facecam corner so the camera never duplicates inside the gameplay box. Save the camera crop layout per streamer channel.
   - **`a_camara` — Speaker Vertical Crop (talking-head / reaction full frame):**
     - Use `detect_faces` + `auto_reframe` (`crop` to `9:16` following the face).

### 3.2 Highlight Extraction (`>= 5` Narrative Clips, `60–90 s`)
1. Run Drift `activity` and `detect_scenes` (`list_scenes` for shot boundaries/scores).
2. Run `drift_gameplay_cli.py transcribe --full --model small --device auto --vad-filter` (with 16 kHz `.f32`/PCM audio via `ffmpeg`) to capture accurate word-level timestamps across the full source.
3. Run `drift_gameplay_cli.py find-highlights --audio-f32 ./out/audio_full.f32 --min-dur 60 --max-dur 90 --min-count 5` combining robust Median/MAD audio energy z-scores, transcript hype/laughter density, `activity`, and `scenes`, placing peaks near `~65–70 %` of each window.
4. **Curate via `highlights.md`:** Read every candidate's full preview before cutting. Drop silent stretches or repeated ambient/radio lines; ensure each selected Reel opens directly on an engaging line/visual hook (avoid graphic violence/blood in the first `7 s` and avoid unmuted copyright radio), ends on a natural payoff sentence, and stays strictly `>= 60 s`.
5. Slice and separate the selected highlights cleanly on the timeline (`2.0 s` gaps optional) with one `add_bookmark` per Reel `#1..#N` (`Reel #N (XXs) - <Topic>`).

### 3.3 Clean Editing Discipline & Subtle Reel-Entry Accent Only
> **Editorial Focus:** Keep the body of every Reel distraction-free. Do **not** spam `camera_shake`, `camera_punch`, mid-clip `zoom_pulse`, or impact hits across dialogue.

- **Boundary micro-fades:** Apply clean `equalPower` micro-fades on foreground clips (`set_fade({in: 0.10..0.15, out: 0.15..0.20})`, `set_fade_curve({curve: "equalPower"})`) to eliminate audio/visual snaps.
- **Entry accent only (`0.0–0.85 s` of each Reel):**
  - Gentle `set_keyframes` settle on `scaleX` / `scaleY` (`interpolation: "smooth"`) right at the Reel's start — e.g. from `[tl_start, base * 1.06]` smoothly to `[tl_start + 0.85, base]` (or `[tl_start, base]` -> `[tl_start + 0.35, base * 1.05]` -> `[tl_start + 0.85, base]`), with no further zoom keyframes later in the Reel.
  - Optional subtle zero-quota `whoosh` (`stock` `sfx` with `quota: null`, e.g. trimmed `0.45–0.75 s`) placed once at `tl_start` on a dedicated bottom `SFX` audio lane (`volume: 0.35–0.40`, `set_fade` `in: 0.02, out: 0.08`, `equalPower`).
- **Internal sub-cuts (only if a Reel stitches two moments):** Clean cut or brief `crossfade` (`0.20–0.25 s`, no flashy swirl/shake effects).
- **Audio loudness target:** Verify/normalize foreground dialogue/gameplay near `~ −14 LUFS` (`TP <= −1.5 dBTP`) via `audio_measure` / `normalize_volume` after arranging clips.

### 3.4 Dual EN/ES Native Karaoke Subtitles (`apply-reel-subs` Option C)
1. Run `build-reel-subs` (first pass without `--translated-json`) to generate `cues_for_translation.json`, `reel_clips_en.json`, and `reels_en.srt` (`1–3` words per cue, breaking cleanly on `[.,;:!?…]` or pauses `> 0.35 s`, matched per Reel via `--inspect-file`). Pass `--corrected-script` to align cleaned transcript lines onto Whisper word timings if needed.
2. Read `cues_for_translation.json`, fix any Whisper misspellings (optional `"text_en"` cleanup), and translate naturally into gamer Spanish (`translated_cues.json`, preserving cue indices).
3. Run `build-reel-subs` (second pass with `--translated-json translated_cues.json`) to emit synchronized `reel_clips_en.json`, `reel_clips_es.json`, `reels_en.srt`, `reels_es.srt`, and `reel_subs_manifest.json`.
4. Run `drift_gameplay_cli.py apply-reel-subs --manifest ./out/subs/reel_subs_manifest.json --primary-lang es --output ./out/subs/apply_subs_result.json`, which executes Drift's native Option C pipeline cleanly with `dry_run: true` before every `atomic: true` batch:
   - Staging-creates **one independent subtitle clip per Reel** via `add_subtitle_clip` (`staging >= dur + 30`), trims each clip to its Reel duration (`set_trim({in: 0, out: reel_dur})`), moves it into exact timeline position (`move_clip({at: reel_start})`), and populates only that Reel's cues converted to **clip-local `[0..reel_dur]` seconds** (`set_subtitle_cues`, required by Drift's player even when `clip.start > 0`).
   - Prepends a second subtitle track (`add_track({type: "subtitle"})`) so `ES` (Track 0, `hidden: false`) and `EN` (Track 1, `hidden: true`) stay completely isolated without colliding.
   - Applies Drift's verified word-by-word yellow karaoke style (`apply_text_preset("karaoke-pop")` + `set_text`: `fontFamily: "Anton"`, `pixelSize: 64`, `letterSpacing: 2`, `textCase: "upper"`, `align: "center"`, `valign: "center"`, `boxEnabled: false`, `accent: {rule: "karaoke", color: "#ffffd400", colorEnabled: true, sizeScale: 1.0}`) and centers in the safe zone (`set_transform({positionX: 540, positionY: 1435})`, cleanly below the FG gameplay frame and well above the bottom 20 % `y >= 1536` TikTok/Reels HUD).
5. Visually verify both tracks via `capture` (checking multiple cue midpoints to confirm yellow word-by-word highlighting tracks live without letter crowding or UI overlap).
