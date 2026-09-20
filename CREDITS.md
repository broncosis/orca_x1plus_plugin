# Credits

## Adapted code

- **OrcaSlicer** — https://github.com/OrcaSlicer/OrcaSlicer — AGPL-3.0 —
  the deterministic filament-id functions in `orca_plugin_x1plus.py` /
  `orca_plugin_x1plus_any.py` / `x1plus_deploy.py`
  (`_system_filament_id`/`_base62_tail`, reimplementing
  `scripts/orca_id_tool.py`'s `generate_filament_id`; `_user_filament_id`,
  reimplementing `src/slic3r/GUI/CreatePresetsDialog.cpp`'s
  `calculate_md5`-based user-filament-id scheme) are ported from
  OrcaSlicer's own source so this plugin computes the exact same ids Orca
  itself would, rather than reading a value that can go stale — see
  `x1plus-orca-filament-plugin-context.md` §17 for why. This project's
  license was changed from GPL-3.0 to AGPL-3.0 to match, per
  GPL-3.0 §13's permitted-combination terms and this project's own
  license-compatibility policy (`.claude/CLAUDE.md`).

## Referenced only (not incorporated as code)

Cited inline in `x1plus-orca-filament-plugin-context.md`:

- **X1Plus** — https://github.com/X1Plus/X1Plus — GPL-3.0 — its D-Bus
  settings interface and shipped `x1plus` CLI are invoked at runtime over
  SSH; no X1Plus source is included here.
- **Bambu-Research-Group/RFID-Tag-Guide** — https://github.com/Bambu-Research-Group/RFID-Tag-Guide
  — referenced for background on the RFID tag format only (not used by
  the plugin, which doesn't touch the RFID tag).

This file should be updated whenever code, macros, configs, or logic from
another project is copied or adapted into this repo, per `.claude/CLAUDE.md`.
