#!/usr/bin/env python3
"""
x1plus_deploy.py — standalone CLI for pushing custom filaments to an
X1Plus-jailbroken Bambu X1/X1C, no Orca Slicer required.

This is the core logic the Orca plugin will eventually wrap. Test it
straight from the command line first -- it's the risky, unverified half
(SSH auth, on-device file format, service restart) and doesn't depend on
anything about Orca's plugin runtime, which none of us can test outside
Orca itself.

Requires: pip install paramiko

--------------------------------------------------------------------------
ONE-TIME SETUP (run once per printer):

    python3 x1plus_deploy.py bootstrap --host <printer-ip>

  Prompts for the root password interactively (never stored, never
  written to disk, held in memory only for this one SSH session), and:
    1. generates a dedicated RSA-4096 keypair in ~/.x1plus_orca_plugin/
       (only if one doesn't already exist there)
    2. logs in with the password once
    3. appends the public key to /root/.ssh/authorized_keys
    4. verifies key-based login works before finishing

  After this, the password is never needed again -- everything else uses
  the key.

--------------------------------------------------------------------------
PUSHING A FILAMENT:

    python3 x1plus_deploy.py push --host <printer-ip> \
        --name "My Custom PLA" --type PLA --vendor MyBrand \
        --temp-min 200 --temp-max 230 \
        --filament-id GFL999 --setting-id GFSL999

  This will:
    1. connect with the key from bootstrap
    2. find whatever filament database is currently active on the
       printer (via the filament.filename / filament.ota_version
       X1Plus settings), download it
    3. strip Bambu's signed-package header if present (detected by
       searching for the zip's PK magic bytes, not a hardcoded offset)
    4. merge your new entry into all nozzle-diameter JSON files inside it
    5. upload the merged zip to /userdata/cfg/filament/ on the printer
       under a name that deliberately does NOT contain ".sig"
    6. point the filament.filename X1Plus setting at it
    7. restart the screen service so bbl_screen picks it up

--------------------------------------------------------------------------
ROLLING BACK:

    python3 x1plus_deploy.py rollback --host <printer-ip>

  Clears filament.filename and restarts the screen service, returning to
  whatever filament.ota_version (Bambu's official downloaded catalog)
  was last set.
"""

import argparse
import getpass
import io
import json
import sys
import zipfile

try:
    import paramiko
except ImportError:
    print("This script needs paramiko: pip install paramiko", file=sys.stderr)
    sys.exit(1)

import os

KEY_DIR = os.path.expanduser("~/.x1plus_orca_plugin")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "id_rsa")
PUBLIC_KEY_COMMENT = "orca-x1plus-plugin"

REMOTE_DEPLOY_DIR = "/userdata/cfg/filament"
REMOTE_DEPLOY_NAME = "orca-plugin-filament.zip"  # deliberately no ".sig"
REMOTE_DEPLOY_PATH = f"{REMOTE_DEPLOY_DIR}/{REMOTE_DEPLOY_NAME}"

NOZZLE_FILES = ["filament-0.2.json", "filament-0.4.json", "filament-0.6.json", "filament-0.8.json"]


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

def ensure_keypair():
    """Generate ~/.x1plus_orca_plugin/id_rsa{,.pub} if it doesn't exist yet.
    Returns (paramiko.RSAKey, public_key_line)."""
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)

    if os.path.exists(PRIVATE_KEY_PATH):
        key = paramiko.RSAKey.from_private_key_file(PRIVATE_KEY_PATH)
    else:
        print(f"No existing key at {PRIVATE_KEY_PATH} -- generating a new RSA-4096 keypair...")
        key = paramiko.RSAKey.generate(4096)
        key.write_private_key_file(PRIVATE_KEY_PATH)
        os.chmod(PRIVATE_KEY_PATH, 0o600)
        print(f"Wrote private key to {PRIVATE_KEY_PATH} (mode 600). Keep this file safe.")

    pubkey_line = f"{key.get_name()} {key.get_base64()} {PUBLIC_KEY_COMMENT}"
    return key, pubkey_line


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def connect_with_password(host, password, username="root"):
    client = paramiko.SSHClient()
    # TOFU: fine for a printer on your own LAN; if you want stricter
    # checking, swap this for load_system_host_keys() + a pinned entry.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=username, password=password, timeout=15,
                    allow_agent=False, look_for_keys=False)
    return client


def connect_with_key(host, key, username="root"):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=username, pkey=key, timeout=15,
                    allow_agent=False, look_for_keys=False)
    return client


def run(client, command, check=True):
    """Run a command over SSH, return (exit_status, stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if check and exit_status != 0:
        raise RuntimeError(f"remote command failed ({exit_status}): {command}\nstdout: {out}\nstderr: {err}")
    return exit_status, out, err


# ---------------------------------------------------------------------------
# Bootstrap: install our public key using the root password, once
# ---------------------------------------------------------------------------

def bootstrap(host, username="root"):
    key, pubkey_line = ensure_keypair()

    print(f"Connecting to {username}@{host} to test whether our key already works...")
    try:
        client = connect_with_key(host, key, username)
        client.close()
        print("Key-based login already works -- nothing to do. "
              "(If you want to re-bootstrap anyway, that's harmless; "
              "just delete the key line from the printer's authorized_keys first.)")
        return
    except Exception:
        pass  # expected on first run

    password = getpass.getpass(f"Root password for {host} (used once, never stored): ")

    client = connect_with_password(host, password, username)
    try:
        run(client, "mkdir -p /root/.ssh && chmod 700 /root/.ssh")
        # Only append if not already present, so re-running bootstrap is safe.
        check_cmd = f"grep -qxF '{pubkey_line}' /root/.ssh/authorized_keys 2>/dev/null"
        _, _, _ = run(client, check_cmd, check=False)
        exit_status, _, _ = run(client, check_cmd, check=False)
        if exit_status != 0:
            append_cmd = (
                f"touch /root/.ssh/authorized_keys && "
                f"echo '{pubkey_line}' >> /root/.ssh/authorized_keys && "
                f"chmod 600 /root/.ssh/authorized_keys"
            )
            run(client, append_cmd)
            print("Public key installed on the printer.")
        else:
            print("Public key was already present on the printer.")
    finally:
        client.close()
        del password  # doesn't really scrub memory in CPython, but signals intent

    print("Verifying key-based login now works...")
    client = connect_with_key(host, key, username)
    client.close()
    print("Bootstrap complete. Future runs won't need the password.")


# ---------------------------------------------------------------------------
# Fetching + merging the on-device filament catalog
# ---------------------------------------------------------------------------

def get_x1plus_setting(client, key):
    """Returns the parsed JSON value of an X1Plus setting, or None if unset."""
    exit_status, out, err = run(client, f"x1plus settings get '{key}' --json", check=False)
    if exit_status != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def sftp_get_bytes(client, remote_path):
    sftp = client.open_sftp()
    try:
        with sftp.open(remote_path, "rb") as f:
            return f.read()
    finally:
        sftp.close()


def sftp_put_bytes(client, remote_path, data):
    sftp = client.open_sftp()
    try:
        with sftp.open(remote_path, "wb") as f:
            f.write(data)
    finally:
        sftp.close()


def strip_signature_header(data):
    """Bambu's signed OTA packages are [custom header incl. signature] +
    [ordinary zip]. Our own previously-pushed overrides are already a
    plain zip. Handle both by searching for the zip local-file-header
    magic rather than assuming a fixed offset."""
    magic = b"PK\x03\x04"
    offset = data.find(magic)
    if offset == -1:
        raise RuntimeError("couldn't find a zip signature (PK\\x03\\x04) anywhere in the fetched file -- "
                            "this doesn't look like a filament database at all")
    if offset != 0:
        print(f"  (stripped {offset}-byte header before the zip payload)")
    return data[offset:]


def fetch_active_catalog(client):
    """Find and download whatever filament database is currently active
    on the printer, returning it as a plain (header-stripped) zip's bytes."""
    filename_setting = get_x1plus_setting(client, "filament.filename")
    if filename_setting:
        print(f"Active override (filament.filename) = {filename_setting}")
        raw = sftp_get_bytes(client, filename_setting)
        return strip_signature_header(raw)

    ota_version = get_x1plus_setting(client, "filament.ota_version")
    if ota_version:
        path = f"/userdata/cfg/filament/{ota_version}"
        print(f"No override set; using official downloaded catalog (filament.ota_version) = {ota_version}")
        raw = sftp_get_bytes(client, path)
        return strip_signature_header(raw)

    raise RuntimeError(
        "Neither filament.filename nor filament.ota_version is set on this printer -- "
        "there's no known catalog file to start from yet. On the printer's touchscreen, "
        "go to Settings > Version > Filament database and download the official database "
        "once, then re-run this."
    )


def merge_entries(catalog_zip_bytes, new_entries):
    """Merge new_entries (dict of {display_name: entry_dict}) into every
    nozzle-diameter JSON file found in catalog_zip_bytes. Returns new zip bytes."""
    src = zipfile.ZipFile(io.BytesIO(catalog_zip_bytes))
    names = [n for n in src.namelist() if n in NOZZLE_FILES]
    if not names:
        raise RuntimeError(f"none of the expected files {NOZZLE_FILES} were found in the fetched catalog "
                            f"(found: {src.namelist()})")

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            catalog = json.loads(src.read(name))
            existing_ids = {v["filament_id"] for k, v in catalog.items() if k not in new_entries}
            for disp_name, entry in new_entries.items():
                if entry["filament_id"] in existing_ids:
                    raise RuntimeError(
                        f"filament_id {entry['filament_id']!r} for {disp_name!r} collides with an "
                        f"existing entry in {name}. Pick a different --filament-id/--setting-id."
                    )
            catalog.update(new_entries)
            out.writestr(name, json.dumps(catalog))
    return out_buf.getvalue()


# ---------------------------------------------------------------------------
# Push + reload
# ---------------------------------------------------------------------------

def restart_screen_service(client):
    print("Restarting the screen service (bbl_screen) so it picks up the change...")
    run(client, "/etc/init.d/S99screen_service restart")


def push(host, new_entries, username="root"):
    key, _ = ensure_keypair()
    client = connect_with_key(host, key, username)
    try:
        print("Fetching the currently active filament catalog...")
        catalog_zip = fetch_active_catalog(client)

        print(f"Merging in: {', '.join(new_entries.keys())}")
        merged_zip = merge_entries(catalog_zip, new_entries)

        run(client, f"mkdir -p {REMOTE_DEPLOY_DIR}")
        print(f"Uploading merged catalog to {REMOTE_DEPLOY_PATH} ...")
        sftp_put_bytes(client, REMOTE_DEPLOY_PATH, merged_zip)

        print("Pointing filament.filename at the new catalog...")
        run(client, f"x1plus settings set filament.filename '{REMOTE_DEPLOY_PATH}' --string")

        restart_screen_service(client)
        print("Done. Give the screen ~10-15s, then check the AMS filament picker, "
              "or Settings > Version > Filament database (should read 'Custom').")
    finally:
        client.close()


def rollback(host, username="root"):
    key, _ = ensure_keypair()
    client = connect_with_key(host, key, username)
    try:
        print("Clearing filament.filename override...")
        run(client, "x1plus settings set filament.filename '' --null")
        restart_screen_service(client)
        print("Rolled back to the official downloaded catalog (filament.ota_version).")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_boot = sub.add_parser("bootstrap", help="one-time: install our SSH key on the printer")
    p_boot.add_argument("--host", required=True)
    p_boot.add_argument("--username", default="root")

    p_push = sub.add_parser("push", help="merge and push a custom filament entry")
    p_push.add_argument("--host", required=True)
    p_push.add_argument("--username", default="root")
    p_push.add_argument("--name", required=True, help="display name shown in the AMS picker")
    p_push.add_argument("--type", required=True, help="e.g. PLA, PETG, ABS, TPU, PETG-CF")
    p_push.add_argument("--vendor", required=True)
    p_push.add_argument("--temp-min", type=int, required=True)
    p_push.add_argument("--temp-max", type=int, required=True)
    p_push.add_argument("--filament-id", required=True, help="must not collide with an existing filament_id")
    p_push.add_argument("--setting-id", required=True)
    p_push.add_argument("--support", action="store_true", help="mark as a support material")
    p_push.add_argument("--hrc", type=int, default=3, help="required_nozzle_HRC (3=stock nozzle, higher=hardened)")
    p_push.add_argument("--chamber-temp", type=int, default=0)
    p_push.add_argument("--vitrification-temp", type=int, default=45)

    p_roll = sub.add_parser("rollback", help="remove the override, restart screen")
    p_roll.add_argument("--host", required=True)
    p_roll.add_argument("--username", default="root")

    args = parser.parse_args()

    if args.cmd == "bootstrap":
        bootstrap(args.host, args.username)
    elif args.cmd == "push":
        entry = {
            args.name: {
                "type": args.type,
                "filament_id": args.filament_id,
                "nozzle_temperature": [args.temp_min, args.temp_max],
                "filament_is_support": "1" if args.support else "0",
                "required_nozzle_HRC": args.hrc,
                "filament_vendor": args.vendor,
                "setting_id": args.setting_id,
                "chamber_temperatures": str(args.chamber_temp),
                "temperature_vitrification": str(args.vitrification_temp),
            }
        }
        push(args.host, entry, args.username)
    elif args.cmd == "rollback":
        rollback(args.host, args.username)


if __name__ == "__main__":
    main()
