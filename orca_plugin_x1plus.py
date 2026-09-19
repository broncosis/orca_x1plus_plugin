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
import threading
import zipfile

import orca

try:
    import paramiko
except ImportError:
    paramiko = None


KEY_DIR = os.path.expanduser("~/.x1plus_orca_plugin")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "id_rsa")
PUBLIC_KEY_COMMENT = "orca-x1plus-plugin"
REMOTE_DEPLOY_DIR = "/userdata/cfg/filament"
REMOTE_DEPLOY_NAME = "orca-plugin-filament.zip"  # deliberately no ".sig"
REMOTE_DEPLOY_PATH = f"{REMOTE_DEPLOY_DIR}/{REMOTE_DEPLOY_NAME}"
NOZZLE_FILES = ["filament-0.2.json", "filament-0.4.json", "filament-0.6.json", "filament-0.8.json"]


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

    def key_login_works(self):
        try:
            c = self._connect_key()
            c.close()
            return True
        except Exception:
            return False

    def bootstrap(self, password, progress=None):
        _, pubkey_line = self._ensure_keypair()
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
        exit_status, out, _ = self._run(client, f"x1plus settings get '{key}' --json", check=False)
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

        raise RuntimeError(
            "No filament database found on the printer yet. On the touchscreen: "
            "Settings > Version > Filament database > download, then try again."
        )

    def _merge(self, catalog_zip_bytes, new_entries):
        src = zipfile.ZipFile(io.BytesIO(catalog_zip_bytes))
        names = [n for n in src.namelist() if n in NOZZLE_FILES]
        if not names:
            raise RuntimeError(f"expected nozzle files not found (got {src.namelist()})")
        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
            for name in names:
                catalog = json.loads(src.read(name))
                existing_ids = {v["filament_id"] for k, v in catalog.items() if k not in new_entries}
                for disp_name, entry in new_entries.items():
                    if entry["filament_id"] in existing_ids:
                        raise RuntimeError(
                            f"filament_id {entry['filament_id']!r} collides with an existing entry in {name}"
                        )
                catalog.update(new_entries)
                out.writestr(name, json.dumps(catalog))
        return out_buf.getvalue()

    def push(self, new_entries, progress=None):
        client = self._connect_key()
        try:
            catalog_zip = self._fetch_active_catalog(client, progress)
            if progress:
                progress(f"Merging in: {', '.join(new_entries.keys())}")
            merged = self._merge(catalog_zip, new_entries)

            self._run(client, f"mkdir -p {REMOTE_DEPLOY_DIR}")
            if progress:
                progress("Uploading merged catalog...")
            self._sftp_put(client, REMOTE_DEPLOY_PATH, merged)

            if progress:
                progress("Updating filament.filename setting...")
            self._run(client, f"x1plus settings set filament.filename '{REMOTE_DEPLOY_PATH}' --string")

            if progress:
                progress("Restarting screen service...")
            self._run(client, "/etc/init.d/S99screen_service restart")
        finally:
            client.close()


def _list_filament_profiles():
    """Enumerate every filament profile Orca knows about (system presets and
    the user's own custom ones), keyed by display name, with the fields our
    AMS entry needs. Verified against OrcaSlicer's PresetBundle Python
    bindings (PluginHostPresets.cpp) -- unlike the old single-preset guess,
    preset.config_value() and the exact key names here are confirmed against
    source, not scraped from docs.

    Returns (profiles, default_name) where profiles maps
    name -> {"type", "vendor", "temp_min", "temp_max", "is_user"}, and
    default_name is the currently-selected preset's name (or "" if that
    can't be determined -- not fatal, the dropdown just opens unselected)."""
    profiles = {}
    default_name = ""
    try:
        bundle = orca.host.preset_bundle()
        collection = bundle.filaments
        for name in collection.preset_names():
            preset = collection.find_preset(name)
            if preset is None:
                continue
            profiles[name] = {
                "type": preset.config_value("filament_type") or "",
                "vendor": preset.config_value("filament_vendor") or "",
                "temp_min": preset.config_value("nozzle_temperature_range_low") or "",
                "temp_max": preset.config_value("nozzle_temperature_range_high") or "",
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
    """Run work_fn(progress_cb) on a background thread while pumping a
    pulsing progress dialog on the calling (main/UI) thread. Returns
    (error_str_or_None). work_fn must not touch any UI objects itself --
    only call progress_cb(str)."""
    style = orca.host.ui.PD_APP_MODAL | orca.host.ui.PD_AUTO_HIDE
    status = {"msg": "Connecting...", "done": False, "error": None}

    def progress_cb(msg):
        status["msg"] = msg

    def worker():
        try:
            work_fn(progress_cb)
        except Exception as e:
            status["error"] = str(e)
        finally:
            status["done"] = True

    with orca.host.ui.create_progress_dialog(title, status["msg"], maximum=0, style=style) as progress_dlg:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while not status["done"]:
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


def _build_push_dialog_html(profiles, default_name):
    # Embedded inside a <script> tag, not an HTML attribute, so only the
    # "</script" escape below is needed (no HTML-attribute quoting concerns).
    profiles_json = json.dumps(profiles).replace("</", "<\\/")
    default_name_json = json.dumps(default_name)
    options_html = _profile_options_html(profiles, default_name)

    return f"""
<html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
<style>
  label {{ display: block; margin-top: 10px; font-size: 12px; color: var(--orca-muted); }}
  select, input {{ width: 100%; box-sizing: border-box; padding: 6px; margin-top: 2px;
          background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); }}
  button {{ margin-top: 16px; padding: 8px 16px; background: var(--orca-accent);
           color: var(--orca-accent-fg); border: none; cursor: pointer; }}
</style>
<label>Printer IP / hostname<input id="host" value=""></label>
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
<label>filament_id (must be unique, e.g. GFL999)<input id="filament_id" value="GFL999"></label>
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

        orca.host.ui.create_window(
            html=_build_push_dialog_html(profiles, default_name),
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

    def _on_submit(self, result):
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

        def work(progress_cb):
            if result.get("password"):
                deployer.bootstrap(result["password"], progress=progress_cb)
            elif not deployer.key_login_works():
                raise RuntimeError(
                    "No stored key works yet for this printer, and no password "
                    "was given. Enter the root password once to bootstrap."
                )
            deployer.push(new_entry, progress=progress_cb)

        error = _run_with_progress("X1Plus Filament Push", work)
        if error:
            orca.host.ui.message(error, title="X1Plus Filament Push failed")
        else:
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

        html = """
        <html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
        <label>Printer IP / hostname<input id="host" style="width:100%%"></label><br>
        <label>Root password (used once, never stored)<input id="password" type="password" style="width:100%%"></label><br>
        <button onclick="orca.submit({host: document.getElementById('host').value, password: document.getElementById('password').value})">Bootstrap</button>
        </body></html>
        """
        orca.host.ui.create_window(
            html=html,
            title="X1Plus: SSH key setup",
            width=360,
            height=220,
            style=orca.host.ui.WINDOW_MODAL,
            on_submit=self._on_submit,
            on_close=lambda: None,
        )
        return orca.ExecutionResult.success("Dialog opened")

    def _on_submit(self, result):
        if not result or not result.get("password"):
            orca.host.ui.message("A root password is required to bootstrap.", title="X1Plus: SSH key setup")
            return

        deployer = X1PlusDeployer(result["host"])
        error = _run_with_progress("X1Plus: SSH key setup", lambda progress_cb: deployer.bootstrap(result["password"], progress=progress_cb))
        if error:
            orca.host.ui.message(error, title="X1Plus: SSH key setup failed")
        else:
            orca.host.ui.message(f"SSH key installed on {result['host']}", title="X1Plus: SSH key setup")


@orca.plugin
class X1PlusPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(BootstrapX1Plus)
        orca.register_capability(PushFilamentToX1Plus)
