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

> **Always check schemas first:** Run `toolbox` on `project`, `subtitles`, `text`, `scene`, and `auto_reframe` / `set_track` / `add_bookmark` on the active Drift build before building `apply` batches.

### 3.1 Canvas Setup & Vertical 9:16 Framing (`1080×1920`)
1. Switch canvas to `1080×1920` via `set_project_setup`.
2. Two clean vertical framing strategies (pick based on gameplay HUD/action layout and confirm with `capture`):
   - **Blurred Full-Frame Stack (default for UI-rich 16:9 games):**
     - Background video lane: scale up to fill height (`1920 / 1080 = 1.778`, e.g. `scaleX: 1.78, scaleY: 1.78`) + add `builtin.effects.gaussian_blur` (or `stylize.blur`) and lower `opacity` slightly (`~0.75`) over dark background (`set_background`).
     - Foreground gameplay lane (above background): center-fit or gentle crop (`scaleX: 0.5625..0.85` depending on safe HUD visibility, `positionX: 540, positionY: 960`), or enable `detect_faces` + `auto_reframe` when a facecam/character focus exists.
   - **Action Punch-In Framing:** Scale `1.2–1.6` centered on key gameplay action (`positionX`/`positionY`), keyframed dynamically if HUD elements shift.

### 3.2 Highlight Extraction (`>= 5` Clips, `60–90 s`)
1. Run Drift `activity` and `detect_scenes` (`list_scenes` for shot boundaries/scores).
2. Run `drift_gameplay_cli.py transcribe --full` to capture word-level timestamps across the entire edited gameplay.
3. Run `drift_gameplay_cli.py find-highlights --min-dur 60 --min-count 5` combining transcript excitement, `activity`, and `scenes`.
4. Slice and separate the selected highlights cleanly on the timeline (leaving a small visual separator gap or placing one `add_bookmark` per Reel `#1..#5+` so the user can export each range via `set_work_area` or `export_video`).

### 3.3 Dynamic Zooms, Effects & SFX
- **Keyframed zoom punch-ins:** On `zoom_cues` emitted by `find-highlights`, animate `scaleX` and `scaleY` with `set_keyframes({"clip": ..., "interpolation": "smooth", "keys": {"scaleX": ..., "scaleY": ...}})` (e.g. `base -> base * 1.12 -> base` over `0.6–1.2 s`), or add `zoom_pulse` / subtle `camera_shake` / `camera_punch` effects at intense moments.
- **Transitions:** Use dynamic transitions on sub-cuts (`radial_zoom_blur`, `simple_zoom`, `cross_zoom_swirl`, or `crossfade`, `0.20–0.30 s`) from `list_transitions`.
- **SFX (`quota: null`):** Search `sfx` marketplace (`motion/whoosh`, `motion/impact`, `ui/pop`) and place short hits synced to zoom punch-ins and transitions on a dedicated `SFX` audio lane (`volume ~0.40–0.60`).

### 3.4 Dual EN/ES Viral Subtitles (`Subtitles EN` & `Subtitles ES`)
1. Run `build-reel-subs` (first pass without `--translated-json`) to generate `reels_en.srt` and `cues_for_translation.json` (`2–4` words per punchy cue, zero overlaps, matched to final Reels timeline coordinates via `--inspect-file` or `--timeline-mode`).
2. Translate `cues_for_translation.json` directly into natural gamer Spanish (`translated_cues.json`, preserving punchiness and cue indices).
3. Run `build-reel-subs` (second pass with `--translated-json translated_cues.json`) to emit synchronized `reels_en.srt` and `reels_es.srt`.
4. Import both `.srt` files as two separate subtitle tracks via `import_subtitle_file` (`Subtitles EN` and `Subtitles ES`).
5. Discover available text styles/animations via `list_text_presets`, `list_text_looks`, and `list_text_animations`, style a cue using `apply_text_preset` / `set_text` / `set_text_animation`, and copy across the track with `apply_text_style_to_all`.
6. Hide one subtitle track (`set_track({"track": <es_or_en_idx>, "hidden": true})`) so the user can export one language cleanly and toggle tracks to export the other.
7. Call `capture` on at least two representative Reel frames (one English visible, one Spanish visible) to confirm safe 9:16 margins (keep captions away from bottom 20% TikTok/Reels UI overlay).
