# X1Plus Orca Filament Plugin

Pushes a custom filament profile into the AMS filament picker on an
X1Plus-jailbroken Bambu X1/X1C, directly from OrcaSlicer, bypassing the lack
of an official third-party channel into Bambu's cloud-synced filament
database. See `x1plus-orca-filament-plugin-context.md` for the full
technical background (RFID/AMS data model, why this is necessary at all).

**How it actually works, confirmed live against a real printer:** the AMS
manual filament picker reads its custom-filament list from
`/config/screen/userFilaments/<nozzle-diameter>.json` — one plain JSON file
per nozzle size, keyed by the short display name shown in the picker. An
earlier version of this project instead overrode the signed *official*
catalog via X1Plus's `filament.filename`/`.ota_version` settings (the
mechanism [X1Plus PR #477](https://github.com/X1Plus/X1Plus/pull/477)
documents) — that write succeeded and the resulting data was verified
correct on disk, but had **no visible effect on the picker**. Manually
adding an entry to `userFilaments` did, immediately. Whatever the
official-catalog override actually governs, it isn't the manual picker's
material list — so that's not what this project does anymore.

## Requirements

- A Bambu X1/X1C running **X1Plus** custom firmware (root SSH access).
- An **OrcaSlicer build with the Python plugin system**. As of writing this
  is not in a stable release — it's being developed on a feature branch (the
  build used for testing was a `PR-1` AppImage). Check
  `File > Plugins` exists in your build at all before proceeding.
- Network access from your desktop to the printer (same LAN).

## 1. Install the plugin

Two ways to get `orca_plugin_x1plus.py` into Orca:

**Option A — Orca's own installer (recommended):**
`File > Plugins > Install plugin`, pick `orca_plugin_x1plus.py` from this
repo. This registers it enabled by default.

**Option B — manual drop:**
Copy the file into its own subfolder under Orca's plugin directory (it must
be the *only* file in that folder):

```
~/.config/OrcaSlicer/orca_plugins/x1plus/orca_plugin_x1plus.py
```

Then in `File > Plugins`, click **Refresh**, find "X1Plus Filament Push" in
the list, and check its **Activate** box (manual drops are disabled by
default, unlike Option A).

Either way, paramiko gets installed automatically into Orca's embedded
Python environment the first time the plugin loads (via its `dependencies`
PEP 723 declaration) — no separate `pip install` needed on your desktop for
using it inside Orca.

If you edit the plugin file after it's already loaded, click **Reload** on
its row in the Plugins dialog (or **Refresh**) to pick up the change — Orca
never needs a restart for this.

## 2. Find the two capabilities

In the Plugins dialog, click the **▶** arrow next to "X1Plus Filament Push"
to expand it and reveal its two capabilities, each with its own **Run**
button:

- **X1Plus: Set up SSH key (run once per printer)**
- **X1Plus: Push filament to AMS**

Alternative: hover the 3D build-plate viewport and press **Space** to open
Orca's Speed Dial quick-launcher, which lists both by name.

## 3. One-time setup: install your SSH key

Run **"X1Plus: Set up SSH key"**. A small form appears asking for the
printer's IP/hostname and its root password. Fill both in and click
**Bootstrap**.

This connects once with the password (typed directly into Orca — it's never
seen by anything else, never written to disk), generates a dedicated
RSA-4096 keypair in `~/.x1plus_orca_plugin/` on your desktop (only if one
doesn't already exist), appends the public key to the printer's
`/root/.ssh/authorized_keys`, and verifies key-based login works. After this
succeeds, you won't need the password again for this printer.

You'll see a couple of one-time permission prompts from Orca's plugin
sandbox the first time the plugin opens a network socket, and the first
time it reads one of your own filament profiles to look up its id (a
"process" permission request — this plugin reads certain files by spawning
`cat` rather than calling Python's `open()` directly, since Orca's sandbox
otherwise unconditionally blocks reading anything under its own config
directory; see the code comments in `orca_plugin_x1plus.py` for the full
explanation). Approve both — each is a one-time approval per Orca launch,
not per file or per push.

## 4. Push a filament

Run **"X1Plus: Push filament to AMS"**. In the form:

1. Enter the printer's IP/hostname (leave the password field blank — you
   already bootstrapped the key in step 3).
2. Pick an existing profile from the **Filament profile** dropdown (your own
   custom profiles are listed separately from Orca's system profiles). This
   auto-fills display name, type, vendor, nozzle temperature range, and —
   when it can actually be recovered for that profile — `filament_id`.
   Alternatively, ignore the dropdown and fill the fields in by hand.
3. Pick the **nozzle diameter** you're targeting (0.2/0.4/0.6/0.8mm). The
   picker's custom-filament list is per nozzle size on the printer.
4. Leave **filament_id** blank to auto-generate a unique one, or type your
   own. Reusing an id that's already used by a *different* entry will ask
   you to confirm before proceeding (rare — these are locally-generated
   ids, not a small curated namespace, so an accidental collision is a hash
   coincidence, not a routine case). Reusing the *same* display name as an
   existing entry is not a collision at all — it just updates that entry.
5. Click **Push to AMS**.

This fetches the current `userFilaments/<nozzle>.json` from the printer (or
starts fresh if that nozzle size has never had a custom filament added),
adds/updates your entry, backs up the pre-existing file once (never
overwriting an earlier backup), uploads the result, and restarts the screen
service (~10-15s) so the AMS picks it up. A message box reports success or
failure.

**Confirm it worked:** check the AMS filament picker on the printer's
touchscreen for your new entry.

## Removing an entry

There's currently no GUI capability for this — use the standalone CLI
instead:

```
python3 x1plus_deploy.py remove --host <printer-ip> --name "My Custom PLA" --nozzle-diameter 0.4
```

This deletes that one entry and restarts the screen service. A full-file
backup (`<file>.orca-plugin-backup`) was already made on the printer the
first time anything here wrote to that nozzle diameter's file — restore it
by hand over SSH if you want to undo everything at once instead.

## Standalone CLI (no Orca required)

Everything the plugin does is also available as a plain CLI,
`x1plus_deploy.py` (`pip install paramiko` first) — useful for testing
outside Orca, scripting, or if you'd rather not type the root password into
any GUI. See the top of that file for `bootstrap` / `push` / `remove` usage.

## Known limitations

- The `userFilaments` entry schema is what Orca's own built-in filament-sync
  feature already writes for filaments you've calibrated there — a
  reasonably complete per-filament record (type, vendor, temps, ids), but
  still not a full slicer print profile. The rest of what Orca knows about
  a filament stays in Orca.
- `setting_id` genuinely doesn't exist for a from-scratch custom filament
  with no official Bambu catalog lineage — Orca itself never assigns one.
  When it can't be found, this plugin reuses `filament_id` as a stand-in,
  since nothing here validates its format or uniqueness.
- No rename: pushing under a new display name for what you consider "the
  same" filament creates a second, separate entry rather than renaming the
  old one. Remove the old one explicitly via the CLI if you want it gone.
