# X1Plus Orca Filament Plugin

Pushes a custom filament profile into the AMS filament picker on an
X1Plus-jailbroken Bambu X1/X1C, directly from OrcaSlicer, bypassing the lack
of an official third-party channel into Bambu's cloud-synced filament
database. See `x1plus-orca-filament-plugin-context.md` for the full
technical background (RFID/AMS data model, the X1Plus override hook, why
this is necessary at all).

## Requirements

- A Bambu X1/X1C running **X1Plus** custom firmware (root SSH access), with
  X1Plus 3.1+ (the `filament.filename` override hook shipped in
  [X1Plus PR #477](https://github.com/X1Plus/X1Plus/pull/477)).
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

You may see a one-time permission prompt from Orca's plugin sandbox the
first time the plugin opens a network socket — approve it.

## 4. Push a filament

Run **"X1Plus: Push filament to AMS"**. In the form:

1. Enter the printer's IP/hostname (leave the password field blank — you
   already bootstrapped the key in step 3).
2. Pick an existing profile from the **Filament profile** dropdown (your own
   custom profiles are listed separately from Orca's system profiles). This
   auto-fills display name, type, vendor, and nozzle temperature range from
   that profile — you can still edit any of these fields afterward.
   Alternatively, ignore the dropdown and fill the fields in by hand.
3. Set a **filament_id** and **setting_id** that don't collide with an
   existing entry in the printer's catalog (the defaults, `GFL999` /
   `GFSL999`, are safe placeholders for a one-off test but pick your own for
   anything you intend to keep).
4. Click **Push to AMS**.

This fetches whichever filament database is currently active on the
printer, merges in your new entry, uploads it, points the
`filament.filename` X1Plus setting at it, and restarts the screen service
(~10-15s) so the AMS picks it up. A message box reports success or failure.

**Confirm it worked:** check the AMS filament picker for your new entry, or
on the printer's touchscreen go to `Settings > Version > Filament database`
— it should read "Custom".

## Rolling back

There's currently no GUI capability for this — use the standalone CLI
instead (see `x1plus_deploy.py`'s own docstring for full usage):

```
python3 x1plus_deploy.py rollback --host <printer-ip>
```

This clears the `filament.filename` override and restarts the screen
service, returning to whichever official catalog was last downloaded via
`filament.ota_version`.

## Standalone CLI (no Orca required)

Everything the plugin does is also available as a plain CLI,
`x1plus_deploy.py` (`pip install paramiko` first) — useful for testing
outside Orca, scripting, or if you'd rather not type the root password into
any GUI. See the top of that file for `bootstrap` / `push` / `rollback`
usage.

## Known limitations

- The AMS catalog format is a lightweight material-identification schema
  (type, vendor, temp range, IDs) — no color, retraction, flow, or other
  print-tuning fields. It's not a full slicer profile; the rest of what
  Orca knows about a filament stays in Orca.
- `filament_id`/`setting_id` collisions with an existing entry are rejected,
  but there's no built-in registry of "IDs you've already used" across
  pushes — keep track of your own.
