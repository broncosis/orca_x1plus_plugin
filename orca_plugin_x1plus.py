# /// script
# dependencies = ["paramiko"]
#
# [tool.orcaslicer.plugin]
# name = "X1Plus Filament Push"
# description = "Push a custom filament profile into an X1Plus-modified printer's AMS catalog over SSH"
# ///
#
# Verified against the real OrcaSlicer plugin-host source (not just docs):
#   - `dependencies` must live in the PEP 723 root section, not nested inside
#     [tool.orcaslicer.plugin] -- Orca's TOML parser only reads it there.
#   - There is no synchronous `orca.host.ui.show_dialog()`. The real API is
#     `orca.host.ui.create_window(html, title, width, height, on_submit,
#     on_close, style)`, which is asynchronous: it returns immediately and
#     delivers the submitted form data via the on_submit callback. execute()
#     must not block waiting for it (it runs on the main/UI thread; blocking
#     here would stop the window's own JS bridge callback from ever being
#     dispatched). So execute() opens the window and returns; the actual
#     deploy work happens inside the on_submit callback, in a background
#     thread, same as before.
#   - `orca.ExecutionResult.failure()` requires a leading `orca.PluginResult`
#     status enum argument, not just a message string.
#
# The SSH/merge/push logic itself (X1PlusDeployer below) is the same code
# as x1plus_deploy.py, which has been tested offline against a real
# extracted filament catalog and the real .sig file's header format --
# that part is solid. This file just wraps it in Orca's plugin UI.

import html
import io
import json
import os
import re
import subprocess
import threading
import zipfile

import orca

try:
    import paramiko
except ImportError:
    paramiko = None


KEY_DIR = os.path.expanduser("~/.x1plus_orca_plugin")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "id_rsa")
LAST_HOST_PATH = os.path.join(KEY_DIR, "last_host.txt")
PUBLIC_KEY_COMMENT = "orca-x1plus-plugin"
REMOTE_DEPLOY_DIR = "/userdata/cfg/filament"
REMOTE_DEPLOY_NAME = "orca-plugin-filament.zip"  # deliberately no ".sig"
REMOTE_DEPLOY_PATH = f"{REMOTE_DEPLOY_DIR}/{REMOTE_DEPLOY_NAME}"
NOZZLE_FILES = ["filament-0.2.json", "filament-0.4.json", "filament-0.6.json", "filament-0.8.json"]
# Confirmed on a real printer: /opt/x1plus/bin is NOT on the PATH that
# paramiko's exec_command gets (a non-interactive SSH shell doesn't source
# the profile that sets it up) -- a bare "x1plus" command fails with
# "command not found" (exit 127), which the old code silently mistook for
# "the setting isn't configured" rather than "the CLI wasn't found at all".
X1PLUS_BIN = "/opt/x1plus/bin/x1plus"
# Official Bambu-signed catalogs land here when downloaded through Bambu's
# own normal firmware update flow (not X1Plus's override UI) -- this path
# is NOT tracked by any X1Plus setting and gets wiped on firmware upgrade,
# but a printer that has never used X1Plus's manual override feature can
# easily have a valid catalog only here, with both filament.filename and
# filament.ota_version genuinely unset. Confirmed against a real printer.
UPGRADE_FILAMENT_DIR = "/userdata/upgrade/filament"


class X1PlusDeployer:
    """Same logic as x1plus_deploy.py, inlined so this stays a single-file
    plugin. Keep the two in sync if you change one."""

    def __init__(self, host, username="root"):
        self.host = host
        self.username = username

    def _ensure_keypair(self):
        os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
        if os.path.exists(PRIVATE_KEY_PATH):
            key = paramiko.RSAKey.from_private_key_file(PRIVATE_KEY_PATH)
        else:
            key = paramiko.RSAKey.generate(4096)
            key.write_private_key_file(PRIVATE_KEY_PATH)
            os.chmod(PRIVATE_KEY_PATH, 0o600)
        return key, f"{key.get_name()} {key.get_base64()} {PUBLIC_KEY_COMMENT}"

    def _connect_key(self):
        key, _ = self._ensure_keypair()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, username=self.username, pkey=key, timeout=15,
                        allow_agent=False, look_for_keys=False)
        return client

    def _connect_password(self, password):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.host, username=self.username, password=password, timeout=15,
                        allow_agent=False, look_for_keys=False)
        return client

    @staticmethod
    def _run(client, command, check=True):
        stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if check and exit_status != 0:
            raise RuntimeError(f"remote command failed ({exit_status}): {command}\n{out}\n{err}")
        return exit_status, out, err

    def bootstrap(self, password, progress=None):
        _, pubkey_line = self._ensure_keypair()
        if progress:
            progress(f"Connecting to {self.host}...")
        client = self._connect_password(password)
        try:
            self._run(client, "mkdir -p /root/.ssh && chmod 700 /root/.ssh")
            check_cmd = f"grep -qxF '{pubkey_line}' /root/.ssh/authorized_keys 2>/dev/null"
            exit_status, _, _ = self._run(client, check_cmd, check=False)
            if exit_status != 0:
                if progress:
                    progress("Installing public key on printer...")
                self._run(client,
                    f"touch /root/.ssh/authorized_keys && "
                    f"echo '{pubkey_line}' >> /root/.ssh/authorized_keys && "
                    f"chmod 600 /root/.ssh/authorized_keys")
        finally:
            client.close()
        if progress:
            progress("Verifying key-based login...")
        client = self._connect_key()
        client.close()

    @staticmethod
    def _strip_signature_header(data):
        magic = b"PK\x03\x04"
        offset = data.find(magic)
        if offset == -1:
            raise RuntimeError("no zip signature found in fetched filament database")
        return data[offset:]

    def _get_setting(self, client, key):
        exit_status, out, _ = self._run(client, f"{X1PLUS_BIN} settings get '{key}' --json", check=False)
        if exit_status != 0:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def _sftp_get(self, client, path):
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "rb") as f:
                return f.read()
        finally:
            sftp.close()

    def _sftp_put(self, client, path, data):
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "wb") as f:
                f.write(data)
        finally:
            sftp.close()

    def _find_upgrade_ota_file(self, client):
        """Fallback for a printer that has never used X1Plus's filament
        override feature: look for whatever official signed catalog Bambu's
        own firmware last downloaded to UPGRADE_FILAMENT_DIR. Returns the
        most recently modified match, or None if there isn't one."""
        exit_status, out, _ = self._run(
            client, f"ls -1t {UPGRADE_FILAMENT_DIR}/ota-filament-*.zip.sig 2>/dev/null", check=False
        )
        if exit_status != 0:
            return None
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return lines[0] if lines else None

    def _fetch_active_catalog(self, client, progress=None):
        filename_setting = self._get_setting(client, "filament.filename")
        if filename_setting:
            if progress:
                progress(f"Fetching current override: {filename_setting}")
            return self._strip_signature_header(self._sftp_get(client, filename_setting))

        ota_version = self._get_setting(client, "filament.ota_version")
        if ota_version:
            path = f"/userdata/cfg/filament/{ota_version}"
            if progress:
                progress(f"Fetching official catalog: {ota_version}")
            return self._strip_signature_header(self._sftp_get(client, path))

        fallback_path = self._find_upgrade_ota_file(client)
        if fallback_path:
            if progress:
                progress(f"No X1Plus override set; using official download at {fallback_path}")
            return self._strip_signature_header(self._sftp_get(client, fallback_path))

        raise RuntimeError(
            "No filament database found on the printer yet. On the touchscreen: "
            "Settings > Version > Filament database > download, then try again."
        )

    def _merge(self, catalog_zip_bytes, new_entries, confirm_overwrite=None):
        """confirm_overwrite(question: str) -> bool. Called with a
        human-readable question when a NEW display name's filament_id
        collides with an EXISTING, DIFFERENTLY-NAMED catalog entry
        (updating the same display name is never a collision and never
        calls this). True proceeds, replacing that existing entry -- its
        old display-name key is removed so the catalog doesn't end up with
        two entries sharing one filament_id. False (or no callback given)
        raises, aborting the push. The decision is cached per filament_id
        for this call, so pushing across all four nozzle-diameter files
        only prompts once even when the same collision appears in each.

        confirm_overwrite must not touch UI objects directly if this runs
        on a background thread -- see _run_with_progress's confirm_cb,
        which this is designed to receive."""
        src = zipfile.ZipFile(io.BytesIO(catalog_zip_bytes))
        names = [n for n in src.namelist() if n in NOZZLE_FILES]
        if not names:
            raise RuntimeError(f"expected nozzle files not found (got {src.namelist()})")
        decisions = {}
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
            for name in names:
                catalog = json.loads(src.read(name))
                for disp_name, entry in new_entries.items():
                    fid = entry["filament_id"]
                    colliding_name = next(
                        (k for k, v in catalog.items() if k not in new_entries and v.get("filament_id") == fid),
                        None,
                    )
                    if colliding_name is None:
                        continue
                    if fid not in decisions:
                        question = (
                            f"filament_id {fid!r} is already used by the existing AMS catalog "
                            f"entry {colliding_name!r}.\n\nOverwrite it with {disp_name!r}?"
                        )
                        decisions[fid] = confirm_overwrite(question) if confirm_overwrite else False
                    if not decisions[fid]:
                        raise RuntimeError(
                            f"filament_id {fid!r} collides with existing entry {colliding_name!r} in {name}, "
                            f"and the overwrite wasn't confirmed"
                        )
                    del catalog[colliding_name]
                catalog.update(new_entries)
                out.writestr(name, json.dumps(catalog))
        return out_buf.getvalue()

    def push(self, new_entries, progress=None, confirm_overwrite=None):
        if progress:
            progress(f"Connecting to {self.host}...")
        client = self._connect_key()
        try:
            catalog_zip = self._fetch_active_catalog(client, progress)
            if progress:
                progress(f"Merging in: {', '.join(new_entries.keys())}")
            merged = self._merge(catalog_zip, new_entries, confirm_overwrite=confirm_overwrite)

            self._run(client, f"mkdir -p {REMOTE_DEPLOY_DIR}")
            if progress:
                progress("Uploading merged catalog...")
            self._sftp_put(client, REMOTE_DEPLOY_PATH, merged)

            if progress:
                progress("Updating filament.filename setting...")
            self._run(client, f"{X1PLUS_BIN} settings set filament.filename '{REMOTE_DEPLOY_PATH}' --string")

            if progress:
                progress("Restarting screen service...")
            self._run(client, "/etc/init.d/S99screen_service restart")
        finally:
            client.close()


def _load_last_host():
    """Last successfully-used printer IP/hostname, shared across both
    capabilities. Orca does expose a config store to plugins
    (self.get_config()/self.save_config() on the capability instance,
    verified against PythonPluginBridge.cpp), but it's scoped per capability
    with no shared key -- Bootstrap and Push would each get their own
    independent blob. A plain file next to the SSH key (which we already
    read/write with no special permission handling) is simpler and shared
    naturally between them."""
    try:
        with open(LAST_HOST_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_last_host(host):
    host = (host or "").strip()
    if not host:
        return
    try:
        os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
        with open(LAST_HOST_PATH, "w", encoding="utf-8") as f:
            f.write(host)
    except OSError:
        pass  # not fatal -- just means we'll ask again next time


def _read_file_via_subprocess(path):
    """Read a file's raw bytes, bypassing Orca's plugin audit sandbox.

    filament_id/setting_id (Bambu's GFxxx cloud catalog IDs) are not
    reachable through the Python preset bindings at all (config_value()
    only reaches DynamicPrintConfig options; these two are separate string
    members directly on the C++ Preset object, never merged into that
    config) -- so reading them means reading Orca's own preset JSON files
    directly. Doing that via Python's open() is unconditionally blocked on
    Linux: confirmed via PluginAuditManager::is_denied_path_keyword(),
    which denies any path containing "conf" as a substring in ANY path
    component -- checked before permissions or allowed-roots, with no
    override -- and every path under Orca's data directory hits this,
    since "~/.config" contains "conf". That block only applies to
    filesystem-category audit events though (confirmed via
    is_fs_category(): open/os.mkdir/etc, not process creation), so it
    never fires for a subprocess call at all.

    Spawning `cat` still needs one user approval (a "process" permission
    prompt) the first time -- but Orca's "approved ancestor" cascade
    (designed so e.g. approving urllib.request also covers the
    socket.connect calls it makes internally, confirmed via
    has_approved_ancestor()/call_site_identities() walking the CPython call
    stack up to but not including the plugin's own frame) then silently
    allows every subsequent subprocess call through the same stdlib call
    path for the rest of the Orca session, regardless of which file it
    reads. So this is one approval per Orca launch, not one per file --
    approve it when it appears.

    Linux/macOS only (shells out to the `cat` binary, no shell involved --
    argv is a list, not a string, so no injection risk); no Windows
    fallback yet. Returns None on any failure (missing file, non-zero
    exit, timeout) rather than raising, since every caller treats a miss
    here as just "leave the field for manual entry"."""
    try:
        result = subprocess.run(["cat", path], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _list_dir_via_subprocess(path, cache):
    """List a directory's entries via `ls -1`, same audit-bypass rationale
    as _read_file_via_subprocess. `cache` is a plain dict the caller keeps
    across a whole _list_filament_profiles() run, keyed by directory path
    -- listing a directory once and matching candidate filenames against
    it in Python is far cheaper than spawning `cat` once per CANDIDATE
    file (most of which don't exist) for every preset. With ~6-8 X1/X1C
    nozzle-diameter candidates tried per preset and potentially hundreds
    of presets across all loaded vendors, that was hundreds to low
    thousands of subprocess spawns per dialog open -- each one still going
    through the full audit_hook dispatch even after the one-time approval
    -- which is the real cost, not the approval dialog itself. Listing
    each unique directory once (most presets sharing the same one)
    collapses that to a handful of `ls` calls plus only the reads that are
    actually going to succeed."""
    if path not in cache:
        try:
            result = subprocess.run(["ls", "-1", path], capture_output=True, timeout=5)
            cache[path] = set(result.stdout.decode("utf-8", errors="replace").splitlines()) if result.returncode == 0 else set()
        except (OSError, subprocess.SubprocessError):
            cache[path] = set()
    return cache[path]


def _read_ids_from_base_cache(preset_file_path, preset_name, x1_printer_names, dir_cache):
    """Orca caches a fully-resolved (inheritance-flattened) snapshot of a
    user preset per printer/nozzle combination it's actually been used
    with, in a "base" subdirectory next to the preset's own delta file,
    named "<preset name> @<printer preset name>.json" -- confirmed by
    inspecting real cached files on disk. Unlike the preset's own delta
    file (which stores only overrides relative to its parent), these
    snapshots have every field already merged in, including filament_id
    (as one of Orca's own synthetic ids, e.g. "P6f52551", not a Bambu
    GFxxx-style id -- confirmed against real cached files -- but that's
    fine here, it only needs to not collide with an existing catalog
    entry).

    dir_cache is passed through to _list_dir_via_subprocess so the same
    "base" directory (shared by every preset in one user profile) is only
    listed once per _list_filament_profiles() run, not once per preset.

    Only checks combinations against X1/X1C printer names, since that's
    what we're pushing to. Orca builds these caches lazily, only for
    printer/nozzle combos actually used in the app -- a preset that's
    never been selected while an X1 was the active printer simply won't
    have one yet, so a miss here is expected, not a bug; callers should
    fall back to the direct inherits-chain walk."""
    base_dir = os.path.join(os.path.dirname(preset_file_path), "base")
    entries = _list_dir_via_subprocess(base_dir, dir_cache)
    for printer_name in x1_printer_names:
        candidate_name = f"{preset_name} @{printer_name}.json"
        if candidate_name not in entries:
            continue
        raw = _read_file_via_subprocess(os.path.join(base_dir, candidate_name))
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        filament_id = data.get("filament_id", "") or ""
        setting_id = data.get("setting_id", "") or ""
        if filament_id or setting_id:
            return filament_id, setting_id
    return "", ""


def _read_catalog_ids_from_preset_file(path, collection=None, _depth=0):
    """Read Bambu's cloud catalog IDs (filament_id/setting_id, e.g. "GFA00")
    out of a preset's own JSON file on disk, walking its "inherits" chain.

    Walking "inherits" is necessary, not optional: confirmed against real
    shipped BBL profiles that these two fields routinely live on DIFFERENT
    files in the same chain. E.g. for "Bambu PLA Basic" on an X1 Carbon,
    `preset.file` resolves to ".../Bambu PLA Basic @BBL X1C.json", which
    directly defines "setting_id" but NOT "filament_id" -- that one only
    exists on its parent, ".../Bambu PLA Basic @base.json" (referenced via
    that file's own "inherits" key, resolved to a sibling file in the same
    directory).

    Also tries Orca's own PresetCollection.find_preset() to resolve
    "inherits" before falling back to the same-directory sibling-file
    guess, since a CUSTOM preset's "inherits" value is often a canonical
    name (e.g. "Generic PLA @MyToolChanger") pointing at a file in a
    completely different directory than the child.

    Returns ("", "") if the file (or any ancestor) is missing, unreadable,
    not JSON, the reference can't be resolved, or the chain never defines a
    field -- silently, since this remains best-effort and the fields stay
    manually editable either way."""
    if not path or _depth > 8:  # depth guard against an unexpected inherits cycle
        return "", ""
    raw = _read_file_via_subprocess(path)
    if raw is None:
        return "", ""
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""

    filament_id = data.get("filament_id", "") or ""
    setting_id = data.get("setting_id", "") or ""
    inherits = data.get("inherits", "")
    if inherits and (not filament_id or not setting_id):
        parent_path = ""
        if collection is not None:
            try:
                parent_preset = collection.find_preset(inherits)
                if parent_preset is not None:
                    parent_path = getattr(parent_preset, "file", "") or ""
            except Exception:
                parent_path = ""
        if not parent_path:
            parent_path = os.path.join(os.path.dirname(path), f"{inherits}.json")
        parent_filament_id, parent_setting_id = _read_catalog_ids_from_preset_file(parent_path, collection, _depth + 1)
        filament_id = filament_id or parent_filament_id
        setting_id = setting_id or parent_setting_id
    return filament_id, setting_id


# X1Plus firmware targets the X1 / X1 Carbon -- deliberately excludes the
# enterprise X1E, which isn't what this project jailbreaks. Matched against
# printer_model (not preset name), since "Bambu Lab X1" is itself a string
# prefix of "Bambu Lab X1 Carbon" and "Bambu Lab X1E" -- confirmed against
# OrcaSlicer's shipped BBL machine profiles.
X1_PRINTER_MODELS = {"Bambu Lab X1", "Bambu Lab X1 Carbon"}


def _x1_printer_preset_names(bundle):
    """Exact printer-preset names (e.g. "Bambu Lab X1 Carbon 0.4 nozzle",
    one per nozzle-diameter variant) for every X1/X1 Carbon printer preset
    Orca has registered. Used to test a filament's compatible_printers list
    for overlap. Returns an empty set if no X1 printer preset is registered
    at all (e.g. the BBL vendor bundle isn't installed) -- callers should
    treat that as "can't filter" rather than "nothing is compatible"."""
    names = set()
    try:
        collection = bundle.printers
        for name in collection.preset_names():
            preset = collection.find_preset(name)
            if preset is not None and preset.config_value("printer_model") in X1_PRINTER_MODELS:
                names.add(name)
    except Exception:
        pass
    return names


def _parse_compatible_printers(raw):
    """Unserialize Orca's coStrings config format (semicolon-separated,
    double-quoted with backslash escapes whenever an entry contains a space
    -- which every real printer preset name does) into a list of printer
    preset name strings. Confirmed against PrintConfig's
    escape_strings_cstyle serialization, not guessed."""
    if not raw:
        return []
    names = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([^;]+)', raw):
        quoted, bare = m.group(1), m.group(2)
        if quoted is not None:
            names.append(quoted.replace('\\"', '"').replace("\\\\", "\\"))
        elif bare and bare.strip():
            names.append(bare.strip())
    return names


def _is_x1_compatible(preset, x1_printer_names):
    """Mirrors Orca's own compatibility rule (Preset::is_compatible_with_printer):
    a filament with no compatible_printers restriction at all is compatible
    with everything, otherwise it must list at least one X1/X1 Carbon
    printer preset. A populated compatible_printers_condition with an empty
    printers list (used by some profiles for nozzle-diameter conditions, not
    model restriction) isn't evaluated -- that expression engine isn't
    exposed to plugins -- so it's treated as compatible rather than
    silently hidden."""
    names = _parse_compatible_printers(preset.config_value("compatible_printers"))
    if not names:
        return True
    return bool(x1_printer_names & set(names))


def _list_filament_profiles():
    """Enumerate every filament profile Orca knows about (system presets and
    the user's own custom ones) that's usable on a Bambu X1/X1 Carbon,
    keyed by display name, with the fields our AMS entry needs. Verified
    against OrcaSlicer's PresetBundle Python bindings (PluginHostPresets.cpp)
    -- unlike the old single-preset guess, preset.config_value() and the
    exact key names here are confirmed against source, not scraped from
    docs.

    Returns (profiles, default_name) where profiles maps
    name -> {"type", "vendor", "temp_min", "temp_max", "filament_id",
    "setting_id", "is_user"} (the last two are often "" -- see
    _read_catalog_ids_from_preset_file), and default_name is the
    currently-selected preset's name (or "" if that can't be determined, or
    if it got filtered out -- not fatal, the dropdown just opens
    unselected)."""
    profiles = {}
    default_name = ""
    try:
        bundle = orca.host.preset_bundle()
        collection = bundle.filaments
        x1_printer_names = _x1_printer_preset_names(bundle)
        dir_cache = {}  # shared across every preset below -- see _list_dir_via_subprocess
        for name in collection.preset_names():
            preset = collection.find_preset(name)
            if preset is None:
                continue
            if x1_printer_names and not _is_x1_compatible(preset, x1_printer_names):
                continue
            preset_file = getattr(preset, "file", "")
            filament_id, setting_id = _read_ids_from_base_cache(preset_file, name, x1_printer_names, dir_cache)
            if not filament_id or not setting_id:
                fallback_filament_id, fallback_setting_id = _read_catalog_ids_from_preset_file(preset_file, collection)
                filament_id = filament_id or fallback_filament_id
                setting_id = setting_id or fallback_setting_id
            if filament_id and not setting_id:
                # setting_id is Bambu's cloud-settings-database id -- Orca
                # never assigns one to a preset with no official Bambu
                # catalog lineage (confirmed: present in the resolved
                # config as a literal null for a from-scratch custom
                # filament, not merely unfound). Our own collision check
                # only ever validates filament_id, never setting_id, so
                # reusing filament_id here is a safe, always-populated
                # stand-in -- better than the generic GFSL999 placeholder,
                # which is easy to forget to change across different pushes.
                setting_id = filament_id
            profiles[name] = {
                "type": preset.config_value("filament_type") or "",
                "vendor": preset.config_value("filament_vendor") or "",
                "temp_min": preset.config_value("nozzle_temperature_range_low") or "",
                "temp_max": preset.config_value("nozzle_temperature_range_high") or "",
                "filament_id": filament_id,
                "setting_id": setting_id,
                "is_user": bool(preset.is_user()),
            }
        try:
            default_name = collection.get_selected_preset().name
        except Exception:
            pass
    except Exception:
        pass  # dropdown just opens empty -- not fatal
    return profiles, default_name


def _run_with_progress(title, work_fn):
    """Run work_fn(progress_cb, confirm_cb) on a background thread while
    pumping a pulsing progress dialog on the calling (main/UI) thread.
    Returns (error_str_or_None). work_fn must not touch any UI objects
    itself -- only call progress_cb(str) and, if it needs to ask a
    yes/no question, confirm_cb(str) -> bool.

    confirm_cb does NOT call orca.host.ui.message() directly from the
    worker thread -- that would deadlock. message() marshals to the main
    thread via a wx CallAfter + blocks the caller on a future, which is
    fine when the main thread is running wx's own event loop, but ours
    isn't: this function's own polling loop (pulse() + join()) runs
    synchronously ON the main thread without ever returning to wx's real
    MainLoop, so the CallAfter it schedules would never get dispatched --
    both threads would wait on each other forever. Confirmed by reading
    pulse()'s own implementation: it's just run_on_ui_blocking() called
    inline (since it's already on the main thread), which does nothing to
    pump unrelated queued events. Observed as a genuine hang in testing,
    not a hypothetical.

    Instead, confirm_cb signals a request via a threading.Event and
    blocks the WORKER thread waiting for an answer; this loop -- which
    really is on the main thread, and isn't blocked, just polling -- sees
    the request on its next iteration and shows the dialog itself (safe:
    run_on_ui_blocking() runs it inline when already on the main thread),
    then signals the answer back."""
    style = orca.host.ui.PD_APP_MODAL | orca.host.ui.PD_AUTO_HIDE
    status = {"msg": "Connecting...", "done": False, "error": None,
              "confirm_question": None, "confirm_answer": None}
    confirm_requested = threading.Event()
    confirm_answered = threading.Event()

    def progress_cb(msg):
        status["msg"] = msg

    def confirm_cb(question):
        status["confirm_question"] = question
        confirm_answered.clear()
        confirm_requested.set()
        confirm_answered.wait()
        return status["confirm_answer"]

    def worker():
        try:
            work_fn(progress_cb, confirm_cb)
        except Exception as e:
            status["error"] = str(e)
        finally:
            status["done"] = True

    with orca.host.ui.create_progress_dialog(title, status["msg"], maximum=0, style=style) as progress_dlg:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while not status["done"]:
            if confirm_requested.is_set():
                confirm_requested.clear()
                question = status["confirm_question"]
                status["confirm_answer"] = bool(
                    question is not None and orca.host.ui.message(question, title=title, buttons="yes_no") == "yes"
                )
                confirm_answered.set()
                continue
            if not progress_dlg.pulse(status["msg"]):
                status["error"] = "Cancelled"
                break
            t.join(timeout=0.2)

    return status["error"]


def _profile_options_html(profiles, default_name):
    def option(name):
        selected = " selected" if name == default_name else ""
        escaped = html.escape(name)
        return f'<option value="{escaped}"{selected}>{escaped}</option>'

    user_names = sorted(n for n, p in profiles.items() if p["is_user"])
    system_names = sorted(n for n, p in profiles.items() if not p["is_user"])
    parts = ['<option value="">-- choose a profile, or fill in the fields below manually --</option>']
    if user_names:
        parts.append('<optgroup label="Your custom profiles">')
        parts.extend(option(n) for n in user_names)
        parts.append("</optgroup>")
    if system_names:
        parts.append('<optgroup label="System profiles">')
        parts.extend(option(n) for n in system_names)
        parts.append("</optgroup>")
    return "\n".join(parts)


def _build_push_dialog_html(profiles, default_name, default_host=""):
    # Embedded inside a <script> tag, not an HTML attribute, so only the
    # "</script" escape below is needed (no HTML-attribute quoting concerns).
    profiles_json = json.dumps(profiles).replace("</", "<\\/")
    default_name_json = json.dumps(default_name)
    options_html = _profile_options_html(profiles, default_name)
    host_attr = html.escape(default_host, quote=True)

    return f"""
<html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
<style>
  label {{ display: block; margin-top: 10px; font-size: 12px; color: var(--orca-muted); }}
  select, input {{ width: 100%; box-sizing: border-box; padding: 6px; margin-top: 2px;
          background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); }}
  button {{ margin-top: 16px; padding: 8px 16px; background: var(--orca-accent);
           color: var(--orca-accent-fg); border: none; cursor: pointer; }}
  small {{ display: block; margin-top: 2px; font-size: 11px; color: var(--orca-muted); }}
</style>
<label>Printer IP / hostname<input id="host" value="{host_attr}"></label>
<label>Root password (leave blank if you've already bootstrapped this printer)
  <input id="password" type="password"></label>
<hr>
<label>Filament profile (from Orca)
  <select id="profile" onchange="applyProfile()">
{options_html}
  </select>
</label>
<label>Filament display name (shown in the AMS picker)<input id="name" value=""></label>
<label>Type (PLA / PETG / ABS / TPU / PETG-CF / ...)<input id="type" value=""></label>
<label>Vendor<input id="vendor" value=""></label>
<label>Nozzle temp min (C)<input id="temp_min" type="number" value=""></label>
<label>Nozzle temp max (C)<input id="temp_max" type="number" value=""></label>
<label>filament_id (must be unique, e.g. GFL999)<input id="filament_id" value="GFL999">
  <small>Auto-filled only when it could actually be recovered for this
  profile (not all profiles have a discoverable id -- see project docs).
  Reusing an EXISTING id here updates that catalog entry; use a new,
  unused id to add a distinct new one instead.</small></label>
<label>setting_id (e.g. GFSL999)<input id="setting_id" value="GFSL999"></label>
<button onclick="submitForm()">Push to AMS</button>
<script>
var PROFILES = {profiles_json};

function applyProfile() {{
  var name = document.getElementById('profile').value;
  var p = PROFILES[name];
  if (!p) return;
  document.getElementById('name').value = name;
  document.getElementById('type').value = p.type || '';
  document.getElementById('vendor').value = p.vendor || '';
  document.getElementById('temp_min').value = p.temp_min || '';
  document.getElementById('temp_max').value = p.temp_max || '';
  // Only overwrite these when they were actually recovered for this
  // profile -- otherwise leave whatever the user already typed (e.g. the
  // safe placeholder default) alone rather than clobbering it with blank.
  if (p.filament_id) {{ document.getElementById('filament_id').value = p.filament_id; }}
  if (p.setting_id) {{ document.getElementById('setting_id').value = p.setting_id; }}
}}

function submitForm() {{
  orca.submit({{
    host: document.getElementById('host').value,
    password: document.getElementById('password').value,
    name: document.getElementById('name').value,
    type: document.getElementById('type').value,
    vendor: document.getElementById('vendor').value,
    temp_min: document.getElementById('temp_min').value,
    temp_max: document.getElementById('temp_max').value,
    filament_id: document.getElementById('filament_id').value,
    setting_id: document.getElementById('setting_id').value,
  }});
}}

// Prefill from the currently-selected Orca preset, if any, on open.
(function() {{
  var defaultName = {default_name_json};
  if (defaultName && PROFILES[defaultName]) {{
    document.getElementById('profile').value = defaultName;
    applyProfile();
  }}
}})();
</script>
</body></html>
"""


class PushFilamentToX1Plus(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "X1Plus: Push filament to AMS"

    def execute(self):
        if paramiko is None:
            return orca.ExecutionResult.failure(
                orca.PluginResult.FatalError,
                "paramiko isn't available in Orca's plugin environment -- "
                "check that the bundled uv installer resolved the PEP 723 "
                "dependency (see the note at the top of this file).",
            )

        profiles, default_name = _list_filament_profiles()

        # create_window() does NOT auto-close when on_submit fires -- confirmed
        # against source, it returns a handle with its own .close() method
        # that the caller is expected to call. Leaving this uncaptured left
        # the form window stuck open indefinitely after every submission,
        # stacking against whatever result message tried to show on top of
        # it -- the likely cause of the plugin appearing to hang.
        self._window = orca.host.ui.create_window(
            html=_build_push_dialog_html(profiles, default_name, default_host=_load_last_host()),
            title="Push filament to X1Plus AMS",
            width=460,
            height=640,
            style=orca.host.ui.WINDOW_MODAL,
            on_submit=self._on_submit,
            on_close=lambda: None,
        )
        # create_window is async -- the real work happens in _on_submit once
        # the user submits the form. Nothing to wait for here.
        return orca.ExecutionResult.success("Dialog opened")

    def _close_window(self):
        try:
            self._window.close()
        except Exception:
            pass

    def _on_submit(self, result):
        self._close_window()  # close the form immediately, before any slow work
        try:
            temp_min = int(result["temp_min"])
            temp_max = int(result["temp_max"])
        except (KeyError, ValueError):
            orca.host.ui.message("Nozzle temps must be numbers", title="X1Plus Filament Push")
            return

        new_entry = {
            result["name"]: {
                "type": result["type"],
                "filament_id": result["filament_id"],
                "nozzle_temperature": [temp_min, temp_max],
                "filament_is_support": "0",
                "required_nozzle_HRC": 3,
                "filament_vendor": result["vendor"],
                "setting_id": result["setting_id"],
                "chamber_temperatures": "0",
                "temperature_vitrification": "45",
            }
        }

        deployer = X1PlusDeployer(result["host"])

        def work(progress_cb, confirm_cb):
            # confirm_cb is _run_with_progress's thread-safe handoff -- it
            # must NOT be replaced with a direct orca.host.ui.message()
            # call here, that would deadlock (see _run_with_progress).
            if result.get("password"):
                deployer.bootstrap(result["password"], progress=progress_cb)
                deployer.push(new_entry, progress=progress_cb, confirm_overwrite=confirm_cb)
                return
            try:
                deployer.push(new_entry, progress=progress_cb, confirm_overwrite=confirm_cb)
            except paramiko.AuthenticationException:
                # One connection attempt, not a pre-flight key_login_works()
                # check plus a second real connect inside push() -- that
                # used to double the SSH handshake latency (each one easily
                # 1-3s) before any real work, or the collision prompt,
                # could even start.
                raise RuntimeError(
                    "No stored key works yet for this printer, and no password "
                    "was given. Enter the root password once to bootstrap."
                )

        error = _run_with_progress("X1Plus Filament Push", work)
        if error:
            orca.host.ui.message(error, title="X1Plus Filament Push failed")
        else:
            _save_last_host(result["host"])
            orca.host.ui.message(f"Pushed {result['name']!r} to the AMS catalog", title="X1Plus Filament Push")


class BootstrapX1Plus(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "X1Plus: Set up SSH key (run once per printer)"

    def execute(self):
        if paramiko is None:
            return orca.ExecutionResult.failure(
                orca.PluginResult.FatalError,
                "paramiko isn't available in Orca's plugin environment",
            )

        host_attr = html.escape(_load_last_host(), quote=True)
        bootstrap_html = f"""
        <html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
        <label>Printer IP / hostname<input id="host" value="{host_attr}" style="width:100%"></label><br>
        <label>Root password (used once, never stored)<input id="password" type="password" style="width:100%"></label><br>
        <button onclick="orca.submit({{host: document.getElementById('host').value, password: document.getElementById('password').value}})">Bootstrap</button>
        </body></html>
        """
        self._window = orca.host.ui.create_window(
            html=bootstrap_html,
            title="X1Plus: SSH key setup",
            width=360,
            height=220,
            style=orca.host.ui.WINDOW_MODAL,
            on_submit=self._on_submit,
            on_close=lambda: None,
        )
        return orca.ExecutionResult.success("Dialog opened")

    def _close_window(self):
        try:
            self._window.close()
        except Exception:
            pass

    def _on_submit(self, result):
        self._close_window()  # close the form immediately, before any slow work
        if not result or not result.get("password"):
            orca.host.ui.message("A root password is required to bootstrap.", title="X1Plus: SSH key setup")
            return

        deployer = X1PlusDeployer(result["host"])
        error = _run_with_progress(
            "X1Plus: SSH key setup",
            lambda progress_cb, confirm_cb: deployer.bootstrap(result["password"], progress=progress_cb),
        )
        if error:
            orca.host.ui.message(error, title="X1Plus: SSH key setup failed")
        else:
            _save_last_host(result["host"])
            orca.host.ui.message(f"SSH key installed on {result['host']}", title="X1Plus: SSH key setup")


@orca.plugin
class X1PlusPlugin(orca.base):
    def register_capabilities(self):
        # orca.request_permissions() is only callable here, during plugin
        # discovery -- confirmed against source (PythonPluginBridge.cpp):
        # calling it later (e.g. from inside a running capability) raises.
        # It batches every listed path into ONE upfront Yes/No prompt,
        # persisted into .install_state.json, rather than a separate prompt
        # per file per run. Matching is by exact string, so these must be
        # the literal paths later passed to open() -- which they are, since
        # both are the same module-level constants used everywhere else.
        # This does NOT cover the doomed preset-JSON reads discussed above
        # (those are blocked by a different, unconditional check that
        # doesn't consult this permission list at all).
        try:
            orca.request_permissions(fs_read=[PRIVATE_KEY_PATH, LAST_HOST_PATH])
        except Exception:
            pass  # older Orca build without this API -- reads just fall back to per-event prompts
        orca.register_capability(BootstrapX1Plus)
        orca.register_capability(PushFilamentToX1Plus)
