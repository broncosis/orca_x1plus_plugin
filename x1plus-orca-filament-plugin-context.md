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
