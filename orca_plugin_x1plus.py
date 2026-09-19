# /// script
# [tool.orcaslicer.plugin]
# name = "X1Plus Filament Push"
# description = "Push a custom filament profile into an X1Plus-modified printer's AMS catalog over SSH"
# dependencies = ["paramiko"]
# ///
#
# NOTE FOR TOMORROW: this file is unverified against a real Orca Slicer
# runtime -- I built it from OrcaSlicer's published plugin API docs, but I
# have no way to actually run Orca myself. The three things most likely to
# need adjusting once you load this into Orca and try it:
#   1. The exact attribute names on orca.host.preset_bundle() for reading
#      the currently-selected filament preset (see _prefill_from_preset()
#      below -- it's wrapped in a broad try/except specifically so a
#      wrong guess there just means blank fields, not a crash).
#   2. orca.host.ui.show_dialog()'s exact JS bridge -- I'm using
#      orca.submit({...}) inside the HTML per the documented API, but
#      double check the console if the dialog doesn't return data.
#   3. Whether Orca's embedded interpreter actually resolves the PEP 723
#      `dependencies = ["paramiko"]` line automatically, or whether
#      paramiko needs to be pip-installed into whatever environment Orca's
#      interpreter uses. If the plugin fails to import paramiko, that's
#      the first thing to check.
#
# The SSH/merge/push logic itself (X1PlusDeployer below) is the same code
# as x1plus_deploy.py, which HAS been tested offline against your real
# extracted filament catalog and the real .sig file's header format --
# that part is solid. This file just wraps it in Orca's plugin UI.

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


def _prefill_from_preset():
    """Best-effort attempt to read the currently-selected filament preset
    from Orca so the dialog can prefill fields. Field names on the preset
    bundle object are a guess -- this must not raise, just return {} if
    wrong so the dialog still opens with blank fields."""
    prefill = {"name": "", "type": "PLA", "vendor": "", "temp_min": "", "temp_max": ""}
    try:
        bundle = orca.host.preset_bundle()
        preset = bundle.filament  # GUESS: attribute name unverified
        config = preset.config    # GUESS: attribute name unverified
        prefill["name"] = getattr(preset, "name", "")
        ftype = config.get("filament_type", None)
        if ftype:
            prefill["type"] = ftype[0] if isinstance(ftype, list) else ftype
        vendor = config.get("filament_vendor", None)
        if vendor:
            prefill["vendor"] = vendor[0] if isinstance(vendor, list) else vendor
        temp_range = config.get("nozzle_temperature_range_low", None)
        temp_range_hi = config.get("nozzle_temperature_range_high", None)
        if temp_range:
            prefill["temp_min"] = str(temp_range[0] if isinstance(temp_range, list) else temp_range)
        if temp_range_hi:
            prefill["temp_max"] = str(temp_range_hi[0] if isinstance(temp_range_hi, list) else temp_range_hi)
    except Exception:
        pass  # fields just stay blank -- not fatal
    return prefill


PUSH_DIALOG_HTML = """
<html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
<style>
  label { display: block; margin-top: 10px; font-size: 12px; color: var(--orca-muted); }
  input { width: 100%%; box-sizing: border-box; padding: 6px; margin-top: 2px;
          background: var(--orca-bg); color: var(--orca-fg); border: 1px solid var(--orca-border); }
  button { margin-top: 16px; padding: 8px 16px; background: var(--orca-accent);
           color: var(--orca-accent-fg); border: none; cursor: pointer; }
</style>
<label>Printer IP / hostname<input id="host" value="%(host)s"></label>
<label>Root password (leave blank if you've already bootstrapped this printer)
  <input id="password" type="password"></label>
<hr>
<label>Filament display name<input id="name" value="%(name)s"></label>
<label>Type (PLA / PETG / ABS / TPU / PETG-CF / ...)<input id="type" value="%(type)s"></label>
<label>Vendor<input id="vendor" value="%(vendor)s"></label>
<label>Nozzle temp min (C)<input id="temp_min" type="number" value="%(temp_min)s"></label>
<label>Nozzle temp max (C)<input id="temp_max" type="number" value="%(temp_max)s"></label>
<label>filament_id (must be unique, e.g. GFL999)<input id="filament_id" value="GFL999"></label>
<label>setting_id (e.g. GFSL999)<input id="setting_id" value="GFSL999"></label>
<button onclick="submitForm()">Push to AMS</button>
<script>
function submitForm() {
  orca.submit({
    host: document.getElementById('host').value,
    password: document.getElementById('password').value,
    name: document.getElementById('name').value,
    type: document.getElementById('type').value,
    vendor: document.getElementById('vendor').value,
    temp_min: document.getElementById('temp_min').value,
    temp_max: document.getElementById('temp_max').value,
    filament_id: document.getElementById('filament_id').value,
    setting_id: document.getElementById('setting_id').value,
  });
}
</script>
</body></html>
"""


class PushFilamentToX1Plus(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "X1Plus: Push filament to AMS"

    def execute(self):
        if paramiko is None:
            return orca.ExecutionResult.failure(
                "paramiko isn't available in Orca's plugin environment -- "
                "see the note at the top of this file (item 3)."
            )

        prefill = _prefill_from_preset()
        prefill.setdefault("host", "")

        result = orca.host.ui.show_dialog(
            html=PUSH_DIALOG_HTML % prefill,
            title="Push filament to X1Plus AMS",
            width=420,
            height=560,
        )
        if not result:
            return orca.ExecutionResult.skipped("Cancelled")

        deployer = X1PlusDeployer(result["host"])

        try:
            temp_min = int(result["temp_min"])
            temp_max = int(result["temp_max"])
        except ValueError:
            return orca.ExecutionResult.failure("Nozzle temps must be numbers")

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

        style = orca.host.ui.PD_APP_MODAL | orca.host.ui.PD_AUTO_HIDE
        with orca.host.ui.create_progress_dialog("X1Plus Filament Push", "Connecting...", maximum=0, style=style) as progress_dlg:
            status = {"msg": "Connecting...", "done": False, "error": None}

            def progress_cb(msg):
                status["msg"] = msg

            def worker():
                try:
                    if result.get("password"):
                        deployer.bootstrap(result["password"], progress=progress_cb)
                    elif not deployer.key_login_works():
                        status["error"] = ("No stored key works yet for this printer, and no password "
                                            "was given. Enter the root password once to bootstrap.")
                        status["done"] = True
                        return
                    deployer.push(new_entry, progress=progress_cb)
                except Exception as e:
                    status["error"] = str(e)
                finally:
                    status["done"] = True

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            while not status["done"]:
                if not progress_dlg.pulse(status["msg"]):
                    return orca.ExecutionResult.skipped("Cancelled")
                t.join(timeout=0.2)

        if status["error"]:
            return orca.ExecutionResult.failure(status["error"])
        return orca.ExecutionResult.success(f"Pushed {result['name']!r} to the AMS catalog")


class BootstrapX1Plus(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "X1Plus: Set up SSH key (run once per printer)"

    def execute(self):
        if paramiko is None:
            return orca.ExecutionResult.failure("paramiko isn't available in Orca's plugin environment")

        html = """
        <html><body style="font-family: var(--orca-font); background: var(--orca-bg); color: var(--orca-fg); padding: 12px;">
        <label>Printer IP / hostname<input id="host" style="width:100%%"></label><br>
        <label>Root password (used once, never stored)<input id="password" type="password" style="width:100%%"></label><br>
        <button onclick="orca.submit({host: document.getElementById('host').value, password: document.getElementById('password').value})">Bootstrap</button>
        </body></html>
        """
        result = orca.host.ui.show_dialog(html=html, title="X1Plus: SSH key setup", width=360, height=220)
        if not result or not result.get("password"):
            return orca.ExecutionResult.skipped("Cancelled")

        deployer = X1PlusDeployer(result["host"])
        try:
            deployer.bootstrap(result["password"])
        except Exception as e:
            return orca.ExecutionResult.failure(str(e))
        return orca.ExecutionResult.success(f"SSH key installed on {result['host']}")


@orca.plugin
class X1PlusPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(BootstrapX1Plus)
        orca.register_capability(PushFilamentToX1Plus)
