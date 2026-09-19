# X1Plus / Orca Slicer Custom Filament Plugin — Project Context

Handoff doc for continuing this in VS Code. Covers: how Bambu's AMS filament
data is stored, how X1Plus's jailbreak exposes an override hook, the exact
on-disk schema (reverse-engineered from a real printer), the plugin design,
and the code already written and partially tested.

**Hardware:** Bambu X1/X1C running X1Plus custom firmware (root SSH access).
**Goal:** An Orca Slicer plugin that pushes a custom filament profile into
the printer's AMS filament picker, bypassing the fact that Orca (a
third-party slicer) has no official channel to write into Bambu's
cloud-synced on-printer filament database.

---

## 1. Background: two separate data stores

- **The RFID tag on each spool** (MIFARE Classic 1K, 16 sectors/64 blocks):
  material ID, detailed type name, color (RGBA), spool weight, diameter,
  dry/bed/nozzle temps, production date, a per-spool "Tray UID", and an
  RSA-2048 signature over the tag data. Reference:
  [Bambu-Research-Group/RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide).
  This is read-only in practice; not what we're modifying.

- **The printer's on-device filament catalog** — a separate database the
  AMS uses to look up nozzle temps etc. once it's identified a spool (or to
  populate the manual filament picker). Normally this is:
  - Downloaded as a Bambu-signed OTA package
    (`ota-filament-vXX.XX.XX.XX-<timestamp>.zip.sig`) from
    `https://public-cdn.bblmw.com/upgrade/device/BL-P001/filament/product/...`
  - Pushed to the printer **only** via Bambu Studio talking to Bambu's
    private cloud API — this is why Orca can't natively update it: Orca's
    custom filament profiles use their own UUIDs, not Bambu's `filament_id`
    scheme, and Orca has no credentialed channel into that cloud API anyway.
  - As of Bambu's 2025 "authorization system" lockdown, third-party slicers
    are further restricted to talking through Bambu Connect (closed-source)
    for basic file transfer/print-start only — this surface was never open.

---

## 2. The on-disk filament database format (reverse-engineered)

Downloaded the real file from the printer: `ota-filament-v02.04.00.02-20251120164926.zip.sig` (7513 bytes).

**Structure:** `[528-byte custom header, magic "BIMH", contains the RSA
signature]` + `[ordinary zip file]`. Found the zip start by searching for
the `PK\x03\x04` magic bytes rather than trusting a fixed offset (offset
happened to be 528 in this instance, but the code always searches for the
magic rather than hardcoding it).

**Inside the zip:** four JSON files, one per nozzle diameter:
`filament-0.2.json` (40 entries), `filament-0.4.json` (87 entries),
`filament-0.6.json`, `filament-0.8.json`. Each is a flat dict keyed by the
exact display name shown in the AMS picker:

```json
{
  "Generic PLA": {
    "type": "PLA",
    "filament_id": "GFL99",
    "nozzle_temperature": [190, 240],
    "filament_is_support": "0",
    "required_nozzle_HRC": 3,
    "filament_vendor": "Generic",
    "setting_id": "GFSL99",
    "chamber_temperatures": "0",
    "temperature_vitrification": "45"
  }
}
```

Notes:
- `filament_id` follows Bambu's `GF<vendor/type letter><number>` scheme
  (`GFA00` = Bambu PLA Basic, `GFU02` = Bambu TPU for AMS, `GFS03` = a
  support material with `filament_is_support: "1"`). 87 distinct IDs in
  the 0.4mm file — plenty of free ID space for custom entries, just don't
  collide with an existing one.
- **No color, no retraction/flow/deep print-tuning fields.** This is a
  lightweight AMS material-identification catalog (type + vendor + temp
  range), not a full slicer print profile. A plugin can only realistically
  push this reduced slice — the rest of what Orca knows about a filament
  stays in Orca.
- No bed-temperature field either.

Extracted files are in this project's context already (delivered earlier in
chat): `filament-0.2/0.4/0.6/0.8.json` (pretty-printed), and
`filament-db-extracted.zip` (the stripped, valid zip).

---

## 3. X1Plus's override hook (from source, not docs — this isn't published anywhere)

Cloned `https://github.com/X1Plus/X1Plus` directly (GitHub's web UI/API
kept failing with provenance errors; `git clone` worked fine) and grepped
`bbl_screen-patch/interpose.cpp`. Also cloned the wiki
(`X1Plus/X1Plus.wiki.git`) and confirmed none of this is documented there.

**`get_resource_path` hook** (~line 780-850 of `interpose.cpp`): reads
X1Plus settings via D-Bus (`x1plus.settings` / `GetSettings`), checks two
keys:

- `filament.ota_version` — a filename looked up under
  `/userdata/cfg/filament/<value>` (the official-download path; persists
  across firmware upgrades unlike `/userdata/upgrade/filament`, which gets
  wiped).
- `filament.filename` — **a literal absolute file path.** If the file
  exists, it's used as the filament resource path outright. No directory
  prefix enforced, no format constraint at this layer.

**Signature bypass** — `bbl_sal_verify_firmware_x2path` is also
intercepted:

```cpp
if (!strstr(s1, ".sig")) {
    // pretending everything is fine since this isn't signed after all.
    return 0;
}
```

If the override filename doesn't contain `.sig`, verification is skipped
entirely — the real Bambu signature check never runs. **This means a
custom filament package needs no signature and no 528-byte header at
all** — just an ordinary zip, named anything without `.sig` in it.

**Setting it:** X1Plus settings go over D-Bus (`x1plus.settings.PutSettings`),
and the shipped CLI wraps this:

```
x1plus settings set filament.filename /userdata/cfg/filament/custom.zip --string
x1plus settings get filament.filename --json     # read back
x1plus settings set filament.filename '' --null  # clear / rollback
```

Source: `images/cfw/opt/x1plus/lib/python/x1plus/client/settings.py` and
`images/cfw/opt/x1plus/lib/python/x1plus/services/x1plusd/settings.py`.

**Reload trigger:** the official "download official database" UI flow
(`bbl_screen-patch/patches/printerui/qml/settings/UpgradeDialog.qml`,
`module == "filament"` branch) forces a reload by toggling
`DeviceManager.maintain.nozzleDiameter` off and back on in QML. For a
manual override set via SSH (no QML access), the equivalent is just
restarting the supervised screen service:

```
/etc/init.d/S99screen_service restart
```

(Confirmed via `images/cfw/etc/init.d/S99screen_service` — it's a plain
`start-stop-daemon`-supervised process, `bbl_screen_patch`, safe to
restart.)

**Provenance of this feature:** [X1Plus PR #477](https://github.com/X1Plus/X1Plus/pull/477),
merged May 4 2025, shipped in X1Plus 3.1. PR description: *"In addition to
being able to manually override the filament database (for those who might
be hacking on the AMS), we add UI to download a recommended version..."*
— confirms the override was an intentional (if undocumented) hook, not an
accident. Fixes [issue #401](https://github.com/X1Plus/X1Plus/issues/401)
("X1Plus not allowing new filaments in AMS").

---

## 4. Orca Slicer's plugin system (relevant API surface)

Orca ships a real plugin system (not just G-code post-processing hooks):
Python, embedded CPython interpreter, packaged as a single `.py` (PEP 723
metadata block) or a `.whl`. Docs:
[Plugin System Overview](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_system),
[Plugin Development](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development),
[Host API](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/host),
[Host UI API](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/host_ui),
[Script capability](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/script),
[Registry (config persistence)](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/registry).

Key points:
- Three capability types: `slicing-pipeline`, `script` (manual "Run" button
  in a Plugins dialog — what we're using), `printer-connection`.
- `orca.host` gives **read-only** access to the live slicer model/presets
  (`orca.host.preset_bundle()`).
- `orca.host.ui`: `message()`, `show_dialog(html, title, width, height)`
  (modal HTML form; page calls `orca.submit({...})` in JS to return data,
  or `None` if dismissed), `create_window()` (non-modal), and
  `create_progress_dialog(title, message, maximum, style)` (context
  manager; `.update()`/`.pulse()` return `False` if the user cancelled).
- Filesystem/network/process-spawn calls are gated by a per-operation
  Yes/No permission prompt ("a permission boundary, not a sandbox") —
  persistent-grant option exists.
- `save_config()`/`get_config()` (Registry API) persist plain JSON with
  **no encryption, no secrets/keyring distinction** — confirmed no built-in
  secure storage exists for plugins.
- `execute()` runs on the main/UI thread — a slow call freezes Orca, hence
  spawning a worker thread + polling a progress dialog rather than blocking.

**Caveat:** the exact attribute names on `orca.host.preset_bundle()` for
reading the current filament preset (`type`, `filament_type`,
`filament_vendor`, temp range, etc.) are **unverified** — pulled from
web-fetched doc summaries, not confirmed against a live Orca session. Code
wraps this in a broad try/except so a wrong guess degrades to blank fields
rather than crashing.

---

## 5. Security design decision: SSH keys, not a stored password

Considered and rejected: a plugin config field holding the printer's root
password. Rejected because Orca's plugin `save_config()` persists plain
JSON with no encryption and no secrets API — a stored root password would
sit in plaintext, readable by anything with access to that directory
(plugin sandboxing is "a permission boundary, not a sandbox").

**Chosen design:**
1. Plugin generates its own dedicated RSA-4096 keypair locally (pure
   client-side crypto, no privilege needed at all — this is unrelated to
   root access on the printer).
2. One-time "bootstrap": user provides the root password once, held in
   memory only for a single SSH session, used to append the plugin's
   public key to `/root/.ssh/authorized_keys` on the printer, then
   discarded. Never written to disk.
3. All subsequent operations authenticate with the private key.
4. **Documented but not yet implemented:** restricting the deploy key in
   `authorized_keys` with a forced `command="..."` option to limit blast
   radius if the key ever leaks (would need a wrapper script on the
   printer, since a forced command overrides both shell *and* SFTP
   subsystem requests — real added complexity, deferred to a v2 hardening
   pass rather than blocking v1).

---

## 6. Code written so far

Two files, deliberately split so the untestable-without-Orca half doesn't
block proving the testable-right-now half:

### `x1plus_deploy.py` — standalone CLI, no Orca dependency
Requires `pip install paramiko`. Subcommands: `bootstrap --host <ip>`,
`push --host <ip> --name ... --type ... --vendor ... --temp-min ...
--temp-max ... --filament-id ... --setting-id ...`, `rollback --host <ip>`.

**Tested and confirmed working (offline, against real data):**
- `strip_signature_header()` — fed the actual downloaded `.sig` file,
  correctly found the 528-byte offset dynamically (searches for
  `PK\x03\x04`, doesn't hardcode the offset) and produced a valid zip.
- `merge_entries()` — fed the real extracted catalog zip, correctly merged
  a new entry (87 → 88 entries in the 0.4mm file), correctly raised on a
  deliberately-collided `filament_id`.

**Not yet tested (needs the live printer):** `bootstrap()`, `push()`,
`rollback()` — the actual SSH/SFTP/exec_command calls. Printer was mid-print
at the time this was written; this is the next concrete step.

### `orca_plugin_x1plus.py` — the Orca plugin wrapper
Same deploy logic inlined (duplicated, not imported — Orca single-file
plugins can't pull in a sibling module). Registers two script capabilities:
- `BootstrapX1Plus` — "X1Plus: Set up SSH key (run once per printer)"
- `PushFilamentToX1Plus` — "X1Plus: Push filament to AMS", with an HTML
  form (host, optional root password, filament name/type/vendor/temps/IDs),
  a background worker thread, and a pulsing progress dialog.

**Three explicitly flagged unverified assumptions** (comments at top of
the file):
1. `orca.host.preset_bundle()` attribute names for prefilling the dialog
   from the currently-selected filament (guarded by try/except).
2. `orca.host.ui.show_dialog()` / `orca.submit()` JS bridge behaving
   exactly as documented.
3. Whether Orca's embedded interpreter actually auto-resolves the PEP 723
   `dependencies = ["paramiko"]` declaration, or whether paramiko needs
   installing some other way into Orca's Python environment.

Both files pass `python3 -m py_compile` (syntax-only; `orca_plugin_x1plus.py`
can't be import-tested without Orca's runtime, which isn't available
outside Orca itself).

---

## 7. Next steps

1. **Test `x1plus_deploy.py` against the live printer** once it's free:
   `bootstrap`, then `push` with a real (or throwaway test) filament entry,
   confirm it shows up in the AMS picker / `Settings > Version > Filament
   database` reads "Custom", then `rollback` to confirm that path too.
2. **Load `orca_plugin_x1plus.py` into a real Orca Slicer** and resolve the
   three flagged unknowns above — expect to need to fix at least the
   preset-bundle attribute names.
3. **Optional hardening:** implement the forced-command restriction on the
   deploy key (needs a small wrapper script on the printer that can both
   accept an SFTP-style file upload and run the settings+restart commands,
   since OpenSSH's `command=` overrides both shell and SFTP subsystem
   requests).
4. **Optional:** fetch the live Bambu CDN filament package
   periodically/on-demand to merge against the *current* official catalog
   rather than a point-in-time snapshot, so third-party additions don't
   go stale as Bambu ships new officially-supported materials. (Already
   partially handled — `fetch_active_catalog()` always pulls whatever's
   currently active on the printer rather than a bundled snapshot.)

---

## 8. Reference links

- [X1Plus/X1Plus](https://github.com/X1Plus/X1Plus) (clone directly if
  GitHub's web UI/API gives provenance errors — that happened repeatedly
  in this session; plain `git clone` always worked)
- [X1Plus PR #477](https://github.com/X1Plus/X1Plus/pull/477)
- [X1Plus Issue #401](https://github.com/X1Plus/X1Plus/issues/401)
- [Bambu-Research-Group/RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)
- [OrcaSlicer Plugin System Overview](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_system)
- [OrcaSlicer Host UI API](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/host_ui)
- [OrcaSlicer Script capability API](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/script)
