# Changelog

Todos los cambios relevantes de este proyecto se documentaran en este archivo.

El formato esta basado en Keep a Changelog y el proyecto utiliza Semantic Versioning.

## [0.0.2] - 2026-11-21

### Changed

- Modo B (`9:16`) fijado al preset canonico de `ClipsParte2`: `solo_juego` (`1.18` sobre fondo `4.5` difuminado `u_blurRadius: 42` y `opacity: 0.72`, `#ff0b0b12`), micro-acento unico de entrada (`0.0-0.85 s` + `01_woosh.flac`), nivelacion via `utility.limiter` manteniendo `volume: 1.0`, y subtitulos nativos `karaoke-pop` bilingues a `y=1435`.
- Documentado en `references/drift-workflows.md` el limite de fragmentacion de `activity()` (`<= 3600 s`, `peaks <= 30`) y calibracion segura de `utility.limiter`.

### Fixed

- `_load_windows` en `scripts/drift_gameplay_cli.py` ahora acepta la clave `"highlights"` ademas de `"reels"`/`"windows"`.
- `find-highlights` ahora lee el esquema real de `activity()` de Drift (`"content"` y arrays planos `"audio"`/`"content"` sin wrapper `"samples"`).

## [0.0.1] - 2026-11-20

### Added

- Inicializacion del proyecto.
