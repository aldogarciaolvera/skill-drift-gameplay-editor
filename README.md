# `drift-gameplay-editor` — Skill para CutWire Drift (Antigravity)

Skill reutilizable para automatizar la edición de gameplays en **CutWire Drift** mediante su servidor MCP local (`/mcp`), combinando análisis local con `ffmpeg`, **YAMNet** y **faster-whisper**.

Soporta dos modos de edición:

1. **Modo A — Gameplay Largo (YouTube 16:9):**
   - Detecta y recorta menús de pausa, pantallas de carga/escritorio y mapas largos manteniendo el audio/video sincronizado (`ripple: true`).
   - Aplica transiciones sutiles (`crossfade` / `dip`) y SFX `whoosh` en saltos temporales.
   - Detecta música con copyright mediante YAMNet + Whisper y mutea **solo** los tramos sin diálogo ni score propio del juego (`set_keyframes` en `volume`).
   - Descarga o reutiliza música _Free To Use_ de Drift (`quota: null`), nivela a `~ -22..-24 LUFS` y rellena los espacios muteados **y tramos de conducción sin radio** (con auto-ducking `duck_under` debajo de diálogos).
   - Marca casos dudosos con `add_bookmark` y genera `youtube_credits.txt`, `chapters.txt` y `edit_report.md`.

2. **Modo B — Reels / Shorts / TikTok (`9:16` vertical, configuración canónica `ClipsParte2`):**
   - Pensado para editar clips verticales limpios desde gameplay ya editado o crudo (o generar primero una hoja editorial completa desde transcripción).
   - Cambia el lienzo a `1080×1920 @ 30 fps`, aplica por defecto el layout `solo_juego` (`scale 1.18` centrado sobre fondo duplicado con `gaussian_blur` `u_blurRadius: 42`, `scale: 4.5` sin barras negras, `opacity: 0.72` y `volume: 0.0` sobre `#ff0b0b12`), o bien `facecam` arriba/juego abajo o `a_camara` vía `auto_reframe`.
   - Analiza audio (`z` robusta mediana/MAD), actividad, escenas y transcripción (`find-highlights`) generando `highlights.md` (`[HH:MM:SS] - [HH:MM:SS]`, Hook 0–3 s, Planteamiento → Nudo → Desenlace) para curar **5 clips narrativos de `60–90 s`** separados por `2.0 s` con clímax hacia ~65–70 % del clip.
   - Mantiene la edición limpia (sin spam de efectos ni sacudidas en mitad del diálogo): aplica **únicamente acento sutil de entrada al inicio de cada Reel** (`0.0–0.85 s`: zoom suave `1.25 -> 1.18` + `01_woosh.flac` `0.35–0.95 s` a `volume: 0.35` con `equalPower`), nivela audio con `utility.limiter` (`ceiling: 0.86`, `−14 .. −16.5 LUFS`, `TP <= −1.25 dBTP`) y respeta **Safe Zones estrictas** (`≥20 %` inferior, `≥15 %` derecha, `≥12 %` superior).
   - Genera e importa **dos pistas independientes de subtítulos nativos karaoke palabra a palabra** (`Track 0` Español visible y `Track 1` Inglés oculta, fuente `Anton` 64 px amarillo `#ffffd400` en `y=1435`) listas para alternar y exportar ambas tandas.

---

## Estructura del Repositorio

```text
drift-gameplay-editor/
├── SKILL.md                       # Instrucciones principales de la skill para el agente
├── README.md                      # Documentación, instalación y guía de uso
├── pyproject.toml                 # Metadatos y dependencias Python
├── .gitignore
├── scripts/
│   └── drift_gameplay_cli.py      # Script CLI multi-subcomando (PEP 723 + uv)
├── references/
│   └── drift-workflows.md         # Recetas MCP, presets, LUFS, encuadre 9:16 y subtítulos
└── tests/
    └── test_cli.py                # Pruebas automatizadas de la CLI
```

---

## Requisitos Previos

1. **CutWire Drift** abierto con el servidor MCP habilitado (la aplicación expone una `URL` local `http://127.0.0.1:<puerto>/mcp` y un `Token` Bearer por sesión).
2. **[`uv`](https://docs.astral.sh/uv/)** instalado y disponible en `PATH`:
   - **Windows (PowerShell):**
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
     ```
   - **macOS / Linux:**
     ```bash
     curl -LsSf https://astral.sh/uv/install.sh | sh
     ```
3. **`ffmpeg` y `ffprobe`** disponibles en `PATH` (por ejemplo, en Windows con `winget install Gyan.FFmpeg`).

> **Nota sobre dependencias Python (`uv`):** `scripts/drift_gameplay_cli.py` incluye un bloque PEP 723 (`# /// script`) y también existe `pyproject.toml`. Al ejecutar `uv run scripts/drift_gameplay_cli.py ...`, `uv` crea y cachea automáticamente un entorno aislado con `faster-whisper`, `tensorflow`, `tensorflow-hub`, `tf-keras` y `numpy` sin modificar tu Python del sistema ni llenar carpetas sincronizadas (como OneDrive) con `.venv/`.

---

## Instalación en Antigravity

Puedes instalar la skill **globalmente** (disponible en todos tus proyectos) o **por proyecto** (compartible en un repositorio con Git).

### Opción 1 — Instalación Global (recomendada para uso personal)

Copia o clona la carpeta dentro del directorio global de skills de Antigravity:

- **Windows (PowerShell):**
  ```powershell
  git clone https://github.com/<tu-usuario>/drift-gameplay-editor.git "$HOME\.gemini\config\skills\drift-gameplay-editor"
  ```
  O copiando desde tu carpeta local:
  ```powershell
  Copy-Item -Recurse -Force "C:\Users\Wall2\OneDrive\Documents\Proyectos\Skills\drift-gameplay-editor" "$HOME\.gemini\config\skills\drift-gameplay-editor"
  ```
- **macOS / Linux:**
  ```bash
  git clone https://github.com/<tu-usuario>/drift-gameplay-editor.git ~/.gemini/config/skills/drift-gameplay-editor
  ```

Antigravity descubre automáticamente cualquier carpeta con `SKILL.md` dentro de `~/.gemini/config/skills/`.

### Opción 2 — Instalación por Proyecto (`.agents/skills/`)

Para usar la skill solo dentro de un proyecto específico (y compartirla con un equipo):

```text
mi-proyecto/
└── .agents/
    └── skills/
        └── drift-gameplay-editor/
            ├── SKILL.md
            ├── pyproject.toml
            ├── scripts/
            │   └── drift_gameplay_cli.py
            └── references/
                └── drift-workflows.md
```

### Opción 3 — Registro con `skills.json` (sin duplicar archivos)

Si mantienes el repositorio clonado en otra carpeta (por ejemplo `C:\Users\<usuario>\Proyectos\Skills`), puedes enlazarlo en `~/.gemini/config/skills.json` o `<tu-proyecto>/.agents/skills.json`:

```json
{
  "entries": [
    {
      "path": "~/Proyectos/Skills",
      "include_only": ["drift-gameplay-editor"]
    }
  ]
}
```

---

## Uso dentro de Antigravity

1. Abre **CutWire Drift** con tu proyecto cargado y obtén la **URL** y el **Token** de la sesión MCP actual.
2. En el chat de Antigravity, pide lo que necesitas (la skill se activa automáticamente por su `description`, o puedes mencionarla por nombre `drift-gameplay-editor`):
   - **Modo A (YouTube 16:9):**
     > "Usa `drift-gameplay-editor` para editar este gameplay largo para YouTube: recorta pausas, mutea música con copyright conservando diálogos, rellena huecos y conducción con música Free To Use y genera créditos/capítulos. URL: `http://127.0.0.1:4731/mcp`, Token: `...`"
   - **Modo B (Reels / Shorts 9:16):**
     > "Usa `drift-gameplay-editor` en modo Reels 9:16 sobre este gameplay: saca al menos 5 clips narrativos de más de 1 minuto con encuadre vertical limpio, solo entrada sutil al Reel (sin exceso de efectos) y subtítulos en inglés y español. URL: `http://127.0.0.1:4731/mcp`, Token: `...`"

> **Seguridad:** Nunca guardes ni subas a GitHub tokens o URLs reales de una sesión en archivos de la skill. Pásalos solo en el prompt o mediante variables de entorno (`DRIFT_MCP_URL` / `DRIFT_MCP_TOKEN`).

---

## Uso directo de la CLI (`scripts/drift_gameplay_cli.py`)

Todos los subcomandos escriben sus resultados en UTF-8 hacia `--output` / `--out-dir` e imprimen en `stdout` únicamente un resumen corto.

```powershell
# 1. Consultar una herramienta de Drift MCP
uv run scripts/drift_gameplay_cli.py mcp --url "http://127.0.0.1:4731/mcp" --token "<TOKEN>" --tool inspect --args-json '{"clips":true,"detail":true}' --output ./out/inspect.json

# 2. Escanear pausas, mapas y pantallas estáticas en dos fases
uv run scripts/drift_gameplay_cli.py scan-pauses --video "./video.mp4" --ref-pause "./pause.png" --ref-map "./map.png" --output ./out/pauses.json

# 3. Detectar ventanas de música vs diálogos con YAMNet
uv run scripts/drift_gameplay_cli.py detect-music --video "./video.mp4" --keep-f32 ./out/audio.f32 --output ./out/music.json

# 4. Transcribir ventanas musicales o el video completo con Whisper
uv run scripts/drift_gameplay_cli.py transcribe --audio ./out/audio.f32 --windows-file ./out/music.json --model small --device auto --output ./out/transcript.json
uv run scripts/drift_gameplay_cli.py transcribe --audio "./edited.mp4" --full --model small --device auto --output ./out/full_transcript.json

# 5. Planificar lotes apply y entregables para YouTube
uv run scripts/drift_gameplay_cli.py plan-youtube --stage cuts --inspect-file ./out/inspect.json --pauses-file ./out/pauses.json --out-dir ./out/plan
uv run scripts/drift_gameplay_cli.py plan-youtube --stage post-cuts --inspect-file ./out/inspect_post.json --pauses-file ./out/pauses.json --music-file ./out/music.json --transcript-file ./out/transcript.json --fill-assets-file ./out/fill_assets.json --out-dir ./out/plan

# 6. Detectar >= 5 highlights (>60 s) y generar highlights.md para curación (entrada limpia por defecto)
uv run scripts/drift_gameplay_cli.py find-highlights --transcript-file ./out/full_transcript.json --audio-f32 ./out/audio.f32 --activity-file ./out/activity.json --min-dur 60 --min-count 5 --output ./out/highlights.json

# 7. Generar subtítulos por Reel EN/ES y empujarlos nativamente a Drift (karaoke palabra a palabra)
uv run scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels.json --words-per-cue 3 --out-dir ./out/subs
uv run scripts/drift_gameplay_cli.py build-reel-subs --transcript-file ./out/full_transcript.json --ranges-file ./out/highlights.json --inspect-file ./out/inspect_reels.json --translated-json ./out/subs/translated_cues.json --words-per-cue 3 --out-dir ./out/subs
uv run scripts/drift_gameplay_cli.py apply-reel-subs --url "http://127.0.0.1:4731/mcp" --token "<TOKEN>" --manifest ./out/subs/reel_subs_manifest.json --primary-lang es --output ./out/subs/apply_subs_result.json
```

---

## Pruebas

Ejecuta la suite de pruebas local (no requiere conexión activa a Drift; usa un servidor JSON-RPC simulado y video sintético con `ffmpeg`):

```powershell
uv run --no-project python -m unittest discover -s tests -v
```

---

## Notas sobre Cuotas y Licencias

- **Drift Marketplace:** La skill utiliza únicamente `stock.music` (proveedor _Free To Use_, sin límite diario) y `sfx` (sin límite diario), evitando `stock.audio` y `stock.video`.
- **Créditos en YouTube:** Si usas pistas de _Free To Use_, recuerda pegar el contenido generado en `youtube_credits.txt` dentro de la descripción de tu video en YouTube.
