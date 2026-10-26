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

## Security and risks

OrcaCloud rates this plugin **high risk**. That's fair: to edit the
printer's filament list, the plugin needs **root access to your printer**,
and if you choose to, it keeps an SSH key that gives it that access. Read
this before installing.

**You choose how much it keeps.** On each push you either:

- **Type the root password and leave "Remember this printer" unticked.**
  The password is used for that one push and then forgotten. No key is
  created, nothing is added to the printer, and the plugin holds no way
  back into your printer afterwards. You type the password every time.
- **Tick "Remember this printer"** (once). The plugin installs an SSH key
  so later pushes need no password. More convenient, but that key is a
  standing root login to the printer (see the risks below).

**What it touches**

| Where | What | Why |
|---|---|---|
| Your computer | `~/.x1plus_orca_plugin/last_host.txt` (last printer address) | Pre-filling the printer address |
| Your computer | `~/.x1plus_orca_plugin/id_rsa` (SSH private key the plugin generates), **only if you remember a printer** | Logging in without the password |
| Your computer | Orca's filament presets, read-only, through Orca's plugin API | Pre-filling the form |
| Network | SSH (port 22) to the printer address you type in | Nothing else: no internet, no telemetry |
| Printer | Appends the plugin's **public** key to `/root/.ssh/authorized_keys`, **only if you remember the printer** | Key-based login |
| Printer | Edits `/config/screen/userFilaments/<nozzle>.json` (backed up the first time), restarts the screen service | Adding your filament to the AMS picker |

It does **not** read or change your own `~/.ssh/` folder, run programs on
your computer, or send anything anywhere except the printer.

**Risks you're accepting**

- **If you remember a printer, the key file is a root login to it.** `id_rsa` is stored
  unencrypted, protected only by file permissions (owner read/write only).
  Anyone or anything that can read it can log in to your printer as root
  from your network. The key isn't restricted to specific commands; it
  allows a full root shell.
- **Your root password is typed into an Orca window.** It's held in
  memory for one connection and never saved,
  but Orca's plugin system is a permission boundary, not a sandbox: other
  installed plugins run in the same Python process.
- **The printer's identity isn't verified.** The plugin accepts whatever
  SSH host key the printer presents. Someone on your local network who
  could impersonate the printer's address could capture the root password
  when you type it. Only use this on a network you trust.
- **It restarts the printer's touchscreen** (about 10–15 seconds) after
  each push. This hasn't been tested mid-print, so don't push while
  printing.
- **It installs [paramiko](https://www.paramiko.org/)** (the SSH library)
  from PyPI into Orca's Python environment on first load.

**Orca's permission prompts.** When the plugin loads, Orca asks once to
let it read/write the two files above and start a background thread. The
first time it connects to a given printer address, Orca asks once more
for that network connection. Approve them if you're OK with the above.

**Revoking access** (only needed if you ever remembered a printer)

1. On the printer (over SSH as root):
   `sed -i '/ orca-x1plus-plugin$/d' /root/.ssh/authorized_keys`
2. On your computer: delete `~/.x1plus_orca_plugin/`.
3. Uninstall the plugin from Orca's Plugins dialog.

Prefer not to type the root password into Orca? The standalone
`x1plus_deploy.py` CLI (see below) does the same setup from a terminal. It
uses the same key in `~/.x1plus_orca_plugin/`, so the key risks above
still apply.

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

- **X1Plus: Push filament to AMS**: what you'll normally use.
- **X1Plus: Set up SSH key (run once per printer)**: optional. Remembers
  a printer without pushing anything. The push form's "Remember this
  printer" box does the same thing.

Alternative: hover the 3D build-plate viewport and press **Space** to open
Orca's Speed Dial quick-launcher, which lists both by name.

## 3. Decide whether to remember the printer

Nothing to set up if you're happy typing the root password on each push.

To skip the password in future, either tick **Remember this printer** the
next time you push with the password, or run **"X1Plus: Set up SSH key"**
(printer address + root password, then **Bootstrap**). Either way the
plugin connects once with the password, generates a dedicated RSA-4096
keypair in `~/.x1plus_orca_plugin/` (only if one doesn't already exist),
appends the public key to the printer's `/root/.ssh/authorized_keys`, and
checks that key login works.

Orca asks once to allow the network connection to each printer address
the first time. See [Security and risks](#security-and-risks) for every
prompt the plugin triggers and why.

## 4. Push a filament

Run **"X1Plus: Push filament to AMS"**. In the form:

1. Enter the printer's IP/hostname. Then either type the root password
   (tick **Remember this printer** if you want to skip it next time), or
   leave the password blank if this printer is already remembered.
2. Pick an existing profile from the **Filament profile** dropdown (your own
   custom profiles are listed separately from Orca's system profiles). This
   auto-fills display name, type, vendor, nozzle temperature range, and
   `filament_id` (computed the same way Orca computes it).
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
- `setting_id` defaults to the same value as `filament_id`. Orca's plugin
  API doesn't expose a preset's real `setting_id`, and the AMS only matches
  on `filament_id`. You can type a different one in the form.
- No rename: pushing under a new display name for what you consider "the
  same" filament creates a second, separate entry rather than renaming the
  old one. Remove the old one explicitly via the CLI if you want it gone.
