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

## ⚠️ Correction (superseding Section 3 below): wrong mechanism

Everything in Section 3 below (the `filament.filename`/`filament.ota_version`
override hook, the signature-bypass zip package) was reverse-engineered
correctly and the write genuinely succeeds — but it turned out **not to be
what the AMS manual filament picker reads**. Confirmed live against a real
printer: overriding the official catalog this way had no visible effect on
the picker at all, while manually adding an entry to
`/config/screen/userFilaments/<nozzle-diameter>.json` (a much simpler plain
JSON file, one per nozzle diameter, keyed by short display name) showed up
immediately. The code has been rewritten around this confirmed-working
mechanism; see `README.md` for current usage. Section 3 is kept below as a
research record — it's real, working code for something else, possibly
useful for a future feature (e.g. affecting RFID-tag-based auto-detection
rather than the manual picker), just not the answer to the original goal.

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

## 7. Next steps (as of the end of session 1 — see Session 2 below for what actually happened)

1. ~~Test `x1plus_deploy.py` against the live printer~~ — done, see §11.
2. ~~Load `orca_plugin_x1plus.py` into a real Orca Slicer~~ — done, see §9-§11.
3. **Still open:** forced-`command=` restriction on the deploy key (v2
   hardening, not blocking).
4. **Superseded:** fetching the live Bambu CDN catalog no longer matters —
   we don't touch the official catalog at all anymore (§10).

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
- [OrcaSlicer plugin-system discussion #14878](https://github.com/OrcaSlicer/OrcaSlicer/discussions/14878)
  — maintainers' own thread; confirms the whole plugin system is
  experimental, nightly-only, "subject to change"
- [Orca Cloud Plugins Guide](https://cloud.orcaslicer.com/wiki/#plugins) —
  the actual hub-submission docs (§13)

---

## 9. Session 2 — live-testing in real Orca, and what was actually wrong

Everything in §4-§6 above was written against **doc summaries**, not source.
Once actually loaded into a real (custom `PR-1` AppImage) Orca build and
exercised against a real printer, several of those assumptions turned out
wrong, found by reading `/home/rob/media/coding projects/OrcaSlicer`
directly (a full source checkout was available locally) rather than
guessing further:

- **`orca.host.ui.show_dialog()` doesn't exist.** Real API is
  `orca.host.ui.create_window(html, title, width, height, on_submit,
  on_close, style)` — async, returns a handle immediately, delivers
  submitted data via the `on_submit` callback later. Rewrote both
  capabilities around this.
- **`create_window()`'s handle does NOT auto-close on submit.** It has its
  own `.close()` the caller must call. Not doing this left the form window
  stuck open after every submission — the actual cause of an early
  "hangs/crashes" report, found by reading the live Orca debug log
  (`~/.config/OrcaSlicer/log/debug_*.log`) and seeing execution go
  completely silent right after a collision was detected, while the rest
  of Orca kept running normally.
- **`orca.ExecutionResult.failure()` requires a leading
  `orca.PluginResult` status enum**, not just a message string.
- **PEP 723 `dependencies` must be in the root `# /// script` block**, not
  nested inside `[tool.orcaslicer.plugin]` — Orca's TOML parser only reads
  the root section for it.
- **A second, separate deadlock**, found the same way (reading the debug
  log + reading `PluginHostUi.cpp`'s `run_on_ui_blocking()` directly):
  calling `orca.host.ui.message()` (a Yes/No collision-confirm prompt)
  directly from the background worker thread deadlocks. `message()`
  marshals to the main thread via `wxTheApp->CallAfter()` and blocks the
  caller on a `std::future` — fine if the main thread is pumping wx's real
  event loop, but ours wasn't: the polling loop (`pulse()` + `join()`) runs
  synchronously ON the main thread without ever returning to `MainLoop()`,
  so the scheduled callback could never be dispatched. Fixed with a
  `threading.Event` handoff instead: the worker signals a question and
  blocks waiting; the polling loop (genuinely on the main thread, not
  blocked) sees the request next iteration and shows the dialog itself.
- **A redundant SSH round-trip:** the push flow was calling a
  `key_login_works()` pre-check (full connect + close) immediately before
  `push()`'s own connect — two full handshakes back to back before any
  real work could start. Removed; `push()` is attempted directly, catching
  `paramiko.AuthenticationException` for the "needs bootstrap" message.

---

## 10. The real answer: `/config/screen/userFilaments/`, not the catalog

**Everything in §3 (the `filament.filename` override) is real, working
code for the wrong target.** The write succeeds, the resulting on-disk
catalog data was verified byte-correct — and it had **zero visible effect
on the AMS manual filament picker.**

Found by testing the actual mechanism the plugin's own logs pointed at
(`bbl_screen: FilamentFeeder::userFilamentInfo() read filamentsFile
"/config/screen/userFilaments/0.4.json"` in `/var/log/syslog.log` on the
printer) instead of assuming the catalog override was already correct:

```json
{
  "Jayo PETG basic": {
    "base_id": null,
    "filament_id": "P61bde26",
    "filament_is_support": false,
    "filament_type": "PETG",
    "filament_vendor": "Jayo",
    "inherits": null,
    "name": "Jayo PETG basic @Bambu Lab X1 Carbon 0.4 nozzle",
    "nickname": null,
    "nozzle_hrc": 3,
    "nozzle_temperature": [220, 270],
    "setting_id": "PFUS61bde26000000",
    "update_time": "2026-09-19 23:02:00",
    "version": "1.10.0.35"
  }
}
```

- One plain JSON file per nozzle diameter (`0.2.json`/`0.4.json`/
  `0.6.json`/`0.8.json`), keyed by the exact short display name. A nozzle
  size that's never had a custom filament synced to it has **no file at
  all** (confirmed: only `0.4.json`/`0.6.json` existed on the real
  printer, not `0.2`/`0.8`) — that's the expected first-use state, not an
  error, and the deployer starts from `{}` in that case.
- `filament_id` here (`"P" + 7 lowercase hex chars`, e.g. `P6f52551`) is
  **Orca's own locally-generated id scheme** — confirmed by finding it
  already populated for several of the user's real custom filaments
  (ELEGOO, SUNLU, iSANMATE, etc.) that had been synced to the printer
  through Orca's own built-in filament-sync feature at some point, in a
  cache at `~/.config/OrcaSlicer/user/<profile>/filament/base/<preset
  name> @<printer preset name>.json` (a fully-resolved,
  inheritance-flattened snapshot Orca builds lazily per printer/nozzle
  combo actually used). This is genuinely different from Bambu's own
  `GFxxx` official catalog namespace (§2) — not a smaller-effort
  substitute, a completely separate id space.
- `setting_id` (`"PFUS" + hex`) is a *different* namespace again, and
  genuinely doesn't exist for a from-scratch custom filament with no
  Bambu-catalog lineage — confirmed by reading a fully-resolved cache file
  directly and finding the field present as a literal `null`, not merely
  unfound. The plugin falls back to reusing `filament_id` as a stand-in
  since nothing validates this field's format.
- Verified manually first (SSH in, hand-edit the JSON, restart the screen
  service, user confirmed on the physical touchscreen it appeared) *before*
  rewriting the deployer code around it — didn't trust the hypothesis
  until it was seen working.

`orca_plugin_x1plus.py` and `x1plus_deploy.py` were both rewritten around
this. Dropped entirely: signature stripping, zip merging, fetching the
whole official catalog, the `filament.filename` setting write — none of it
is needed. `x1plus_deploy.py`'s `rollback` subcommand (cleared
`filament.filename`) became `remove` (deletes one named entry from
`userFilaments`) since the old semantics don't apply to a mechanism that
never touched a global setting. A one-time full-file backup
(`<file>.orca-plugin-backup`) is made before the first write to each
nozzle diameter's file, never overwriting an earlier backup.

---

## 11. Orca's plugin security sandbox (`PluginAuditManager`) — and a real bypass

Needed to read fields (`filament_id`/`setting_id`) that
`preset.config_value()` can't reach at all (they're plain string members
directly on the C++ `Preset` object, never merged into the
`DynamicPrintConfig` that binding reads from — confirmed via
`PluginHostPresets.cpp`, no `.def_readonly()` for either field anywhere).
The only path is reading Orca's own preset JSON files directly.

Doing that via Python's `open()` is **unconditionally blocked on Linux**,
confirmed via `PluginAuditManager::is_denied_path_keyword()`
(`src/slic3r/plugin/PluginAuditManager.cpp`): it denies any path
containing `"conf"`/`"cert"`/`"secret"` as a **substring in any path
component**, checked *before* permissions or allowed-roots are even
consulted, with no override. `~/.config/OrcaSlicer` contains `"conf"` (the
`.config` component) — so this denies reading literally anything under
Orca's own data directory, including the preset files, the resolved-preset
cache, and even a plugin's own nominally-allowed storage folder, since
they're all under that same directory. Reproduced directly: dozens of
`[AUDIT BLOCKED] ... reason=denied path` lines in the live debug log for
every attempted read. This looks like an unintended side effect of
`default_denied_path_keywords()` targeting literal `conf`/`config` files,
not a deliberate policy against reading these specific paths — worth
flagging upstream.

**The bypass that actually works:** that keyword-deny check only applies
to filesystem-category audit events (`is_fs_category()`: open/mkdir/etc.).
Spawning a subprocess (`subprocess.run(["cat", path], ...)`) is a
different category (`ProcessCreate`) that never goes through it at all.
It still needs one user approval (a "process" permission prompt) the first
time, but Orca's "approved ancestor" call-site cascade — built so e.g.
approving `urllib.request` also covers the `socket.connect` calls it makes
internally (`has_approved_ancestor()`/`call_site_identities()`, which
walks the CPython call stack up to but not including the plugin's own
frame) — then silently allows every subsequent subprocess call through the
same stdlib call path for the rest of the Orca session, regardless of
which file it reads. So it's one approval per Orca launch, not one per
file. `_read_file_via_subprocess()`/`_list_dir_via_subprocess()` in
`orca_plugin_x1plus.py` implement this, with directory listings cached
across one `_list_filament_profiles()` run (see below) rather than probing
every candidate file individually.

This is an intentional, discussed-with-the-user tradeoff (it routes around
a real security boundary, even though the "conf" collision looks
unintentional) — not something to repeat casually elsewhere in this
codebase without the same explicit conversation.

**Performance note:** the naive version of this (try every X1/X1C
nozzle-diameter candidate for every filament preset across every loaded
vendor bundle) was hundreds to low thousands of subprocess spawns per
dialog open — each still going through the full audit dispatch even after
the one-time approval, since the ancestor cascade skips the permission
prompt but not the dispatch itself. Fixed by listing each shared directory
once (`ls -1`, cached in a dict for the whole run) instead of probing per
candidate file: measured 30 naive attempts collapsing to 4 real subprocess
calls against real data.

**`orca.request_permissions(fs_read=[...])`** does exist and does work —
but only for paths that aren't hit by the keyword-deny check to begin with
(i.e. not our own `~/.config/OrcaSlicer` reads). It's used for the
plugin's own small files (`~/.x1plus_orca_plugin/{id_rsa,last_host.txt}`):
called once in `register_capabilities()` (the only context it's valid in —
calling it later raises), it batches every listed path into one upfront
Yes/No dialog instead of a scattered per-event prompt at runtime.

---

## 12. X1/X1 Carbon filtering, host memory, and other smaller fixes

- **Filament dropdown filtered to X1/X1 Carbon**, matched by
  `printer_model` (not preset-name substring — `"Bambu Lab X1"` is itself
  a string prefix of `"Bambu Lab X1 Carbon"`/`"Bambu Lab X1E"`).
  Deliberately excludes X1E (X1Plus targets X1/X1C). Falls back to showing
  everything if no X1 printer preset is registered at all, rather than
  filtering down to nothing.
- **Printer host is remembered** across runs
  (`~/.x1plus_orca_plugin/last_host.txt`), saved after a successful
  bootstrap or push, prefilled into both dialogs. (Orca does expose a
  config store to plugins — `self.get_config()`/`self.save_config()` — but
  it's scoped per-capability with no shared key, so Bootstrap and Push
  would each get an independent blob; a shared file was simpler.)
- **`/opt/x1plus/bin` is not on the `PATH`** that paramiko's
  `exec_command` gets (a non-interactive SSH shell doesn't source the
  profile that sets it up) — a bare `x1plus` command silently failed with
  "not found" (exit 127), which earlier code mistook for "the setting
  isn't configured." No longer relevant to the rewritten push flow (§10
  doesn't call the `x1plus` CLI at all), but real and worth remembering if
  any future code shells out to it again.
- **Collision confirmation**: keyed by `filament_id`, only fires for a
  genuine collision (a different `short_name` reusing another entry's
  id) — updating the same `short_name` is just an ordinary overwrite, no
  prompt, since `short_name` is the file's own unique key.

---

## 13. GitHub + the OrcaCloud plugin hub

- Repo is public: <https://github.com/broncosis/orca_x1plus_plugin>.
- The plugin system itself (including the hub) is **merged into upstream
  OrcaSlicer `main`** as of `baa91282e3`/`080f27f602` (2026-09-11 /
  2026-09-19) — past the latest stable tag (`v2.4.2`, 2026-07-06) and even
  past this checkout's stale `nightly-builds` tag (2026-07-16, likely just
  not re-fetched locally). Confirmed via the maintainers' own thread,
  [discussion #14878](https://github.com/OrcaSlicer/OrcaSlicer/discussions/14878):
  "an initial version is now available for testing in the latest nightly
  builds... still at an early stage... experimental." Not yet in a
  numbered stable release.
- **Hub submission** (from <https://cloud.orcaslicer.com/wiki/#plugins>):
  sign into OrcaCloud → Plugins → "Shared Plugins" → the `+` button →
  drag in `.whl`/`.py` files (up to 20, 100MB each) → fill in name
  (auto-parsed from filename)/description/version/type/compatible Orca
  version/changelog/tags → toggle "Public Plugin" to list it on the hub →
  "Create plugin." GitHub-repo auto-publish-on-release is also supported
  as an alternative to manual uploads. No documented review gate before
  publication.
- **Filenames must end in an OS/arch suffix before the extension**, even
  for a plain `.py` (confirmed by hitting this error live: "Plugin
  filenames must end with a supported OS and arch target before .whl or
  .py, like `my_plugin_win_x86_64.whl`"). Supported: `macosx_arm64` /
  `macosx_x86_64` / `macosx_universal` / `macosx`, `linux` /
  `linux_arm64` / `linux_x86_64`, `win_arm64` / `win_x86_64` / `win`, and
  the universal `any`. Since this plugin is pure Python + paramiko and its
  one POSIX-only feature (§11's subprocess trick) already degrades
  gracefully to manual entry when unavailable, uploaded as
  `orca_plugin_x1plus_any.py` (a renamed copy of the real file — the repo
  keeps the plain name for development).
- Status as of this session: upload was in progress, not yet confirmed
  live on the hub.

---

## 14. Confirming the round trip: does Orca resolve a pushed filament back to the right preset?

Once a custom filament is selected on an AMS tray on the touchscreen, does
Orca (reading the printer's live MQTT state) correctly recognize it as the
same preset it came from, rather than showing it as unrecognized? Traced
the full mechanism in source rather than guessing:

1. Printer broadcasts each AMS tray's material identity as `tray_info_idx`
   in its MQTT device-state message.
2. `DevFilaSystem.cpp:660`: Orca stores this verbatim into
   `DevAmsTray::setting_id` (an internal naming choice — despite the field
   name, the code comment there literally says *"curr_tray->setting_id is
   our OF [Orca Filament] id"*).
3. `Plater.cpp:5680`: `wxGetApp().preset_bundle->get_filament_by_filament_id(tray.setting_id)`
   is called with that value.
4. `PresetBundle.cpp:938 get_filament_by_filament_id()`: a flat linear scan
   over every loaded filament preset, returning the first one whose
   `Preset::filament_id` **exactly string-matches**. No printer-name
   filter is applied at this call site (that parameter is optional and
   omitted here).

**Confirmed with real historical evidence, not just theory:** raw MQTT
payloads already captured earlier in this session (before any of this was
being deliberately investigated) show `"tray_info_idx":"P5ab7e86"` for a
tray loaded with "Matter3d PLA Basic" — and `P5ab7e86` is exactly that
filament's own `filament_id` in `userFilaments`. That's an existing,
already-working instance of this exact round trip, for a filament Orca
itself synced via its built-in feature.

**Why this should also hold for our pushes:** `_list_filament_profiles()`
in `orca_plugin_x1plus.py` sources `filament_id` directly from Orca's own
already-loaded preset for whichever profile you pick in the dropdown (via
the subprocess-based reader, §11) — it is not a fabricated new value. So
the chain is: Orca's own preset for "Jayo PETG basic" already has
`filament_id == "P61bde26"` in memory → we push `"P61bde26"` into
`userFilaments` under that same name → printer reports
`tray_info_idx: "P61bde26"` once that material is selected on a tray →
`get_filament_by_filament_id("P61bde26")` scans loaded presets and finds
the same "Jayo PETG basic" preset it originally came from. It should match
by construction, not by luck — but this has **not yet been visually
confirmed** end to end (select the pushed filament on a real/virtual AMS
tray, then check Orca's own AMS panel shows the correct preset name).
**Next session: do that check.**

Caveat worth remembering: `_generate_filament_id()`-sourced ids (used when
a profile's own id can't be recovered, or the user leaves the field blank)
are *not* sourced from any existing Orca preset — a filament pushed that
way will show up correctly in the AMS picker (confirmed, §10) but will
**not** round-trip back to a matching Orca preset later, since no local
preset carries that freshly-generated id. That's an inherent limitation of
auto-generation, not a bug — there's no existing preset to link back to in
that case.

---

## 15. Capturing real screenshots of the touchscreen over SSH

No `fbgrab`/`fbcat` on this firmware, but there's a raw framebuffer device
that works directly:

```
/dev/fb0 — confirmed 720x1280, 32 bits/pixel, stride 2880 (720*4, no padding)
```

```python
import paramiko
from PIL import Image

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username="root", pkey=key, timeout=15, allow_agent=False, look_for_keys=False)

width, height = 720, 1280
size = width * height * 4
stdin, stdout, stderr = client.exec_command(f"dd if=/dev/fb0 bs={size} count=1 2>/dev/null")
raw = stdout.read()  # exactly `size` bytes, confirmed
client.close()

img = Image.frombuffer("RGBA", (width, height), raw, "raw", "RGBA", 0, 1)
img.rotate(270, expand=True).save("screenshot.png")  # RGBA channel order + 270° rotation both confirmed correct
```

- Pixel format is **RGBA** (confirmed by comparing against BGRA — RGBA
  produced correct, natural colors).
- The raw buffer is captured in **portrait** orientation and needs a
  **270°** rotation to match what's physically shown — matches
  `QT_QPA_LINUXFB_ROTATION=270` found in `bbl_screen`'s own environment
  (`/proc/<pid>/environ`) back in an earlier session.
- `dd if=/dev/fb0 bs=<exact size> count=1` reads a single consistent
  frame; reading via SFTP's `sftp.open()` was not tried and may not work
  at all against a character device — `exec_command` + raw stdout bytes
  (do NOT `.decode()`) is the confirmed-working path.

**Used this and found a real, unresolved problem, not just a screenshot:**
the captured frame showed the X1Plus boot splash, not the normal UI, even
several seconds after `bbl_screen` had already been running — and
`uptime` showed the system had been up ~19 hours, ruling out "still on a
fresh boot." `/var/log/syslog.log` around the same time showed continuous
`netService: command failed: Operation not permitted` and
`device_gate: ... pub failed ... @mqtt` errors. Ruled out an actual network
outage (`wlan0` had the correct IP, `10.0.1.149`, SSH itself worked fine —
the errors were specifically about `eth0`, which this printer doesn't use
at all, so likely a red herring) and ruled out a stuck print
(`ps aux` showed no print/gcode process running, so a power cycle would be
safe if needed). **Left unresolved, handed to the user to check the
physical screen and decide on a power cycle** — an SSH-only screen-service
restart clearly wasn't sufficient to recover it, so if it's really stuck,
the fix is probably at the printer itself, not something to keep
attempting remotely.

---

## 16. Bug found: pushed material name was the whole profile name, not just the material

User confirmed live (profiles do land in the AMS picker) but reported the
display name looked "kind of odd" — the full profile name rather than
just the material. Root cause confirmed against the real OrcaSlicer source
tree (`/home/rob/media/coding projects/OrcaSlicer`), not guessed:

- `PluginHostPresets.cpp:95-100`'s `preset_names()` binding returns
  `Preset::name` verbatim.
- `Preset::name` for essentially every printer/vendor-specific filament
  preset is **not** a plain material name — it's
  `"<material> @<printer preset name>[ <nozzle> nozzle]"`. Confirmed
  against real shipped files, e.g.
  `resources/profiles/BBL/filament/eSUN/eSUN PLA+ @BBL X1C 0.2 nozzle.json`
  (`"name": "eSUN PLA+ @BBL X1C 0.2 nozzle"` inside). 1552 of 1596 files
  under `BBL/filament/` use this convention — the norm, not an edge case.
- `_build_push_dialog_html()`'s `applyProfile()` JS was prefilling the
  editable "Filament display name" field directly from this raw preset
  name. `_on_submit()` then built the AMS entry's own `"name"` field as
  `f"{short_name} @Bambu Lab X1 Carbon {nozzle_diameter} nozzle"` — i.e.
  appending a *second* printer/nozzle suffix on top of the one already
  baked into the preset name. Net result on the printer: a `userFilaments`
  key/display name like `"eSUN PLA+ @BBL X1C 0.2 nozzle"` (or worse, with
  both suffixes stacked in the `"name"` field) instead of just
  `"eSUN PLA+"`.

**Fix:** added `_material_display_name()` (splits on the literal `" @"`
delimiter, returns the preset name unchanged if it has no such suffix —
safe for a plain user-typed custom filament name too). `_list_filament_profiles()`
now includes a `material_name` field per profile; `applyProfile()` prefills
the display-name input from that instead of the raw dropdown key. The
dropdown's own option text/value is left untouched (still the full raw
preset name, since that's needed to disambiguate nozzle/printer variants
of the same material in the list, and to key `PROFILES[...]` lookups and
`find_preset()`). Applied to both `orca_plugin_x1plus.py` and the hub
upload copy `orca_plugin_x1plus_any.py` (kept byte-identical, as before).

**Not yet re-tested against the live printer** — next session should
push a filament with a known "@..."-suffixed source profile (e.g. an
eSUN or Bambu system profile, not just a from-scratch custom one) and
confirm the AMS picker now shows just the material name.

---

## 17. Bug found: pushed filament_id used a retired Orca id scheme, so the AMS panel fell back to generic

User reported that after selecting a pushed filament on an AMS tray, Orca's
own AMS/tray panel showed it as a generic material (color and material type
came through fine, specific product name did not).

Root cause, confirmed against `/home/rob/media/coding projects/OrcaSlicer`
(the user's own fork, `broncosis/OrcaSlicer`, branch
`feat/lane-data-filament-id-matching`, sitting on top of a very recent
`upstream/main` — verified this checkout has real, unmodified upstream
history via `git branch -r --contains <hash>` before trusting anything read
from it, since one commit's message ("content-addressed mint tooling,
succession runtime") initially looked suspicious enough to double-check):

- `docs/HLSD/filament_id.md` (rewritten by upstream commit `4aa0e1d60b`,
  "Translate filament ids at the printer boundary", 2026-09-06) documents
  that OrcaSlicer's system filament catalog now assigns every non-BBL
  vendor filament a content-addressed id: `"OF" + base62_6(uuid5(...,
  "filament_product/<vendor>/<type>/<name>"))` — "No vendor is exempt from
  the filament_id rule." This superseded an older scheme (confirmed via
  `git log -S` on `check_ams_filament_valid`/`get_filament_by_filament_id`,
  landing in upstream commit `2c0867619c`, 2026-07-04).
- This plugin's `_read_ids_from_base_cache()` (§9-§11) reads a **locally
  cached, resolved snapshot** of a filament preset that Orca itself writes
  lazily and never invalidates against catalog changes. Every filament this
  plugin (or the user's earlier manual SSH test, §10) had ever pushed to
  the real printer carried an id from that stale cache — provably the
  *old*, pre-migration scheme: `hashlib.md5(material_name)[:7]` prefixed
  with `"P"`, matching `CreatePresetsDialog.cpp`'s actual (deterministic,
  not random) formula for a from-scratch user filament byte-for-byte
  against all 19 real entries found on the printer (e.g. `md5("Jayo PETG
  basic")[:7]` == `"61bde26"`, exactly the id that had been pushed).
- The doc states the consequence explicitly: *"An id that changes is not
  forwarded anywhere: a tray or record still holding the old value falls
  back to matching by material type until the user re-selects the
  filament."* `PresetBundle::get_filament_by_filament_id()` (still a plain
  first-match linear scan, per §14) simply finds no loaded preset with the
  stale id and returns no match, and the surrounding matcher then falls
  back to a system `Generic <type>` preset by material type — exactly the
  reported symptom.

**Fix (`orca_plugin_x1plus.py`, `orca_plugin_x1plus_any.py`,
`x1plus_deploy.py`):** stopped reading `filament_id` from disk entirely.
Reimplemented Orca's own two minting formulas straight from source
(`_system_filament_id()` for the `"OF..."` scheme, `_user_filament_id()`
for the `"P" + md5(...)[:7]` scheme) and compute the id from data this
plugin already has (`vendor`, `type`, and the material name stripped of its
`@...` suffix via `_material_display_name()`, now matching Orca's own
`BASE_NAME_RE` exactly — an optional, not required, leading space before
`@`). This is immune to cache staleness by construction: it's a pure
function of the same triple Orca itself hashes, so it can never drift the
way a lazily-cached snapshot can. `setting_id` (Bambu's separate
cloud-settings id, confirmed in §14 to never be consulted by the AMS
matcher) is untouched — still best-effort, still read from the same disk
cache as before, since it isn't what was actually broken.

**Live remediation:** recomputed and re-pushed the corrected `filament_id`
for all 19 real entries already on the printer (both `0.4.json` and
`0.6.json`) in one pass, using each entry's own already-recorded
`filament_vendor`/`filament_type`/key as the hash input (no need to query
Orca at all for this part — the data was already sitting in each JSON
entry). Also removed two more `@`-suffixed bugged-name duplicates
(`CC3D PC Basic @Bambu Lab X1 Carbon 0.4 nozzle`, pushed a second time
through the plugin before it had been Reloaded in Orca to pick up the §16
name fix) rather than trying to salvage them.

**Not yet visually confirmed against the live AMS panel** — next step is
for the user to Reload the plugin in Orca (picks up both this fix and
§16's), then check that an existing tray (e.g. Jayo PETG basic) now
resolves to its real preset instead of a generic one, without needing to
re-push anything (the corrected ids are already live on the printer).

---

## 18. Actually-leftover bad profile: the abandoned §3 override was never rolled back

User pushed back on §17's cleanup ("you never removed the bad profiles from
the printer") after the `userFilaments` JSON files had already been
verified clean (checked `git`-style, i.e. read the files directly, not the
touchscreen -- the touchscreen itself was separately found stuck on the
X1Plus boot splash mid-investigation, which turned out to be an unrelated
red herring the user explicitly redirected away from: "check the files not
the screen").

Checking wider than just `userFilaments` turned up a real leftover: the
**§3 mechanism (the signed-catalog-override hook, abandoned in §10 in favor
of `userFilaments`) had never been rolled back**.
`x1plus settings get filament.filename --json` still returned
`"/userdata/cfg/filament/orca-plugin-filament.zip"` -- a stale override
package dated Sep 19, left over from before the project pivoted mechanisms,
still referenced by a live X1Plus setting. Cleared it
(`x1plus settings set filament.filename '' --null`), deleted the leftover
zip, and restarted the screen service. `filament.ota_version` was never
set at all (unaffected).

Lesson: when a project pivots away from a mechanism it already deployed
live, explicitly roll back what was deployed -- `x1plus_deploy.py`'s old
`rollback` subcommand (cleared `filament.filename`) was dropped when it
became `remove` (§10), since the new semantics didn't need it for
`userFilaments` -- but that also meant nothing left ever cleared the
*old* mechanism's leftover state once the pivot happened, on either the
real printer or in the tooling.

---

## 19. A screenshot dead end, a reload dead end, and a real regression

Three more findings from live-testing §16-§18's fixes against the real
printer and a real Orca session, in the order they came up:

**The `/dev/fb0` screenshot technique from §15 was reading a dead buffer.**
Confirmed by checking `bbl_screen`'s own environment and open file
descriptors on the printer: `QT_QPA_FB_DRM=1` is set, and `/proc/<pid>/fd`
showed `/dev/dri/card0` open, not `/dev/fb0`. The screen renders through
DRM/KMS; `/dev/fb0` is never written to by the live UI at all, so every
`dd if=/dev/fb0` capture this session (and in §15) returned whichever
static image was last in that buffer at early boot -- not a hang, just the
wrong device. The user caught this by sending a real phone photo of the
touchscreen, which showed live, correct content (a per-tray material-edit
screen) at the exact moment the fb0 grab showed the boot splash. `modetest`
is present on the printer and confirms the live framebuffer (id 63, AR24,
720x1280) is allocated by `bbl_screen`, but nothing on-device exists to
dump it (no `modetest --dump-framebuffers`, no gcc to build a small
DRM-ioctl dumper, no ffmpeg kmsgrab) -- capturing the *real* screen content
remotely was not solved this session; the phone-photo workaround is what
actually worked.

**No "Reload" option in Orca's Plugins dialog.** Traced to
`evaluate_action_policy()` in `PluginsDialog.cpp`: a plugin registered as a
*cloud* plugin (subscribed from the OrcaCloud hub -- which this one was,
per the §13 hub upload) only ever gets a "Reinstall" action, which
re-pulls from the hub, never a "Reload" that would pick up local file
edits. "Reload" is local-plugin-only. Fix: delete/unsubscribe the
cloud-tracked copy in the Plugins dialog (removes only the local install,
confirmed via the same source not to touch the hub upload), then
`File > Plugins > Install plugin` pointed at the local `.py` file directly
-- registers it as a local plugin, which supports Reload for every future
edit.

**A real regression, self-inflicted.** After landing §17's fix, all 18
pre-existing `userFilaments` entries had their `filament_id` bulk-rewritten
to the new `_system_filament_id()` (`"OF..."`) scheme, on the unverified
assumption that every one of them was an OrcaFilamentLibrary system
preset. User reported filaments that matched correctly *before* this
session now also fell back to generic -- i.e. the bulk rewrite made things
worse, not better. Checking the user's actual Orca profile
(`~/.config/OrcaSlicer/user/<id>/filament/`) showed the assumption was
wrong for at least one entry: "CC3D PC Basic" is genuinely user-created
(no OFL/base-cache lineage), so its correct id is the *other* formula,
`_user_filament_id()` (`"P" + md5(name)[:7]`) -- confirmed once it was
re-pushed through the now-fixed plugin (which reads `preset.is_user()`
live and picks the right formula) and the user confirmed it resolved
correctly in Orca. The 18 pre-existing entries were reverted to their
exact original `filament_id` values (captured from this session's own
earlier verification output, before they were ever overwritten) rather
than guessed at a second time.

**Lesson, stated plainly:** computing an id from a *catalog entry's own
recorded* vendor/type/name (as the revert-then-bulk-fix script did) is not
the same as computing it from the *live, currently loaded Orca preset* (as
the plugin itself does at push time). The catalog can drift from Orca's
current presets for reasons that have nothing to do with any bug here --
e.g. the user renaming or reorganizing presets over time. Also found live:
the printer's "Matter3d PLA Basic" / "Matter3d PETG proformance" catalog
entries correspond to real OrcaFilamentLibrary system presets (base-cache
snapshots only, no delta file -- same pattern as Jayo/eSUN), while the
user separately has their own **distinct, user-created** "M3d ..." preset
family (`M3d - Pla Basic`, `M3d pla`, `M3d_Petg`, `M3d
PETG@stealthchanger`, `M3dPLA @stealthchanger`) that doesn't correspond to
the catalog names at all. Left for the user to manually consolidate in
Orca; the correct fix for any of these once sorted out is to re-push
through the plugin's dropdown (reads the live, post-cleanup preset), never
to hand-compute or hand-edit a `filament_id` from outside Orca again.
