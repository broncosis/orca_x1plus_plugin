#!/usr/bin/env python3
"""
x1plus_deploy.py — standalone CLI for pushing custom filaments to an
X1Plus-jailbroken Bambu X1/X1C, no Orca Slicer required.

This is the core logic the Orca plugin wraps. Test it straight from the
command line first -- it's the risky, unverified half (SSH auth, on-device
file format, service restart) and doesn't depend on anything about Orca's
plugin runtime.

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
        --temp-min 200 --temp-max 230 --nozzle-diameter 0.4

  This will:
    1. connect with the key from bootstrap
    2. fetch /config/screen/userFilaments/<nozzle-diameter>.json from the
       printer (starting fresh with {} if that nozzle diameter has never
       had a custom filament added -- confirmed on a real printer: only
       nozzle sizes actually used have a file at all)
    3. add/update your entry, keyed by --name
    4. back up the pre-existing file once (<file>.orca-plugin-backup),
       never overwriting an earlier backup
    5. upload the updated file
    6. restart the screen service so bbl_screen picks it up

  This targets /config/screen/userFilaments/, confirmed live against a
  real printer to be what the AMS manual filament picker actually reads.
  An earlier version of this tool instead overrode the signed official
  catalog via X1Plus's filament.filename/.ota_version settings (the
  mechanism X1Plus PR #477 documents); that write succeeded and the
  resulting data was verified correct on disk, but had no visible effect
  on the picker. Manually adding an entry to userFilaments did,
  immediately -- whatever the official-catalog override actually
  governs, it isn't the manual picker's material list.

--------------------------------------------------------------------------
REMOVING AN ENTRY:

    python3 x1plus_deploy.py remove --host <printer-ip> \
        --name "My Custom PLA" --nozzle-diameter 0.4

  Deletes that one entry from the userFilaments file (if present) and
  restarts the screen service. A full-file backup was already made the
  first time this tool ever wrote to that nozzle diameter's file
  (<file>.orca-plugin-backup) -- restore it by hand over SSH if you want
  to undo everything at once rather than one entry at a time.
"""

import argparse
import datetime
import getpass
import json
import secrets
import sys

try:
    import paramiko
except ImportError:
    print("This script needs paramiko: pip install paramiko", file=sys.stderr)
    sys.exit(1)

import os

KEY_DIR = os.path.expanduser("~/.x1plus_orca_plugin")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "id_rsa")
PUBLIC_KEY_COMMENT = "orca-x1plus-plugin"

USER_FILAMENTS_DIR = "/config/screen/userFilaments"
NOZZLE_DIAMETERS = ["0.2", "0.4", "0.6", "0.8"]


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


def generate_filament_id():
    """A locally-unique filament id matching Orca's own observed scheme
    for user filaments: "P" + 7 lowercase hex characters (e.g. "P6f52551")
    -- confirmed against real entries already synced to a printer by
    Orca's own built-in mechanism."""
    return "P" + secrets.token_hex(4)[:7]


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
# userFilaments read/write
# ---------------------------------------------------------------------------

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


def fetch_user_filaments(client, nozzle_diameter):
    """Returns the parsed dict from userFilaments/<nozzle_diameter>.json,
    or {} if the file doesn't exist yet -- the normal, expected state for
    a nozzle diameter that's never had a custom filament synced to it
    (confirmed on a real printer: only sizes actually used have a file at
    all)."""
    path = f"{USER_FILAMENTS_DIR}/{nozzle_diameter}.json"
    try:
        raw = sftp_get_bytes(client, path)
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Push + remove + reload
# ---------------------------------------------------------------------------

def restart_screen_service(client):
    print("Restarting the screen service (bbl_screen) so it picks up the change...")
    run(client, "/etc/init.d/S99screen_service restart")


def push(host, short_name, entry, nozzle_diameter, username="root"):
    key, _ = ensure_keypair()
    client = connect_with_key(host, key, username)
    try:
        remote_path = f"{USER_FILAMENTS_DIR}/{nozzle_diameter}.json"
        print(f"Fetching {remote_path} ...")
        data = fetch_user_filaments(client, nozzle_diameter)

        if short_name not in data:
            fid = entry["filament_id"]
            colliding_name = next((k for k, v in data.items() if v.get("filament_id") == fid), None)
            if colliding_name is not None:
                answer = input(
                    f"filament_id {fid!r} is already used by existing entry {colliding_name!r}. "
                    f"Add {short_name!r} as a separate entry anyway? [y/N] "
                )
                if answer.strip().lower() not in ("y", "yes"):
                    raise RuntimeError(
                        f"filament_id {fid!r} collides with existing entry {colliding_name!r}; aborted."
                    )

        data[short_name] = entry

        run(client, f"mkdir -p {USER_FILAMENTS_DIR}")
        # One-time safety net: back up the pre-existing file before ever
        # overwriting it, but only the first time -- never clobber an
        # earlier backup with a later (already-modified) copy. Trailing
        # "; true" so a missing source file or an already-existing backup
        # (neither an error) doesn't trip run()'s check=True.
        run(client, f"test -f {remote_path} && test ! -f {remote_path}.orca-plugin-backup && "
                    f"cp {remote_path} {remote_path}.orca-plugin-backup; true")
        print(f"Uploading updated {remote_path} ...")
        sftp_put_bytes(client, remote_path, json.dumps(data, indent=4).encode("utf-8"))

        restart_screen_service(client)
        print("Done. Give the screen ~10-15s, then check the AMS filament picker.")
    finally:
        client.close()


def remove(host, short_name, nozzle_diameter, username="root"):
    key, _ = ensure_keypair()
    client = connect_with_key(host, key, username)
    try:
        remote_path = f"{USER_FILAMENTS_DIR}/{nozzle_diameter}.json"
        data = fetch_user_filaments(client, nozzle_diameter)
        if short_name not in data:
            print(f"{short_name!r} isn't present in {remote_path} -- nothing to remove.")
            return
        del data[short_name]
        print(f"Uploading updated {remote_path} ...")
        sftp_put_bytes(client, remote_path, json.dumps(data, indent=4).encode("utf-8"))
        restart_screen_service(client)
        print(f"Removed {short_name!r} from {remote_path}.")
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

    p_push = sub.add_parser("push", help="add or update a custom filament in the AMS picker")
    p_push.add_argument("--host", required=True)
    p_push.add_argument("--username", default="root")
    p_push.add_argument("--name", required=True, help="short display name shown in the AMS picker")
    p_push.add_argument("--type", required=True, help="e.g. PLA, PETG, ABS, TPU, PETG-CF")
    p_push.add_argument("--vendor", required=True)
    p_push.add_argument("--temp-min", type=int, required=True)
    p_push.add_argument("--temp-max", type=int, required=True)
    p_push.add_argument("--nozzle-diameter", choices=NOZZLE_DIAMETERS, default="0.4")
    p_push.add_argument("--filament-id", help="defaults to an auto-generated unique id if omitted")
    p_push.add_argument("--setting-id", help="defaults to match --filament-id if omitted")
    p_push.add_argument("--support", action="store_true", help="mark as a support material")
    p_push.add_argument("--hrc", type=int, default=3, help="nozzle_hrc (3=stock nozzle, higher=hardened)")

    p_rm = sub.add_parser("remove", help="remove one entry from the AMS picker")
    p_rm.add_argument("--host", required=True)
    p_rm.add_argument("--username", default="root")
    p_rm.add_argument("--name", required=True, help="short display name to remove")
    p_rm.add_argument("--nozzle-diameter", choices=NOZZLE_DIAMETERS, default="0.4")

    args = parser.parse_args()

    if args.cmd == "bootstrap":
        bootstrap(args.host, args.username)
    elif args.cmd == "push":
        filament_id = args.filament_id or generate_filament_id()
        setting_id = args.setting_id or filament_id
        entry = {
            "base_id": None,
            "filament_id": filament_id,
            "filament_is_support": bool(args.support),
            "filament_type": args.type,
            "filament_vendor": args.vendor,
            "inherits": None,
            "name": f"{args.name} @Bambu Lab X1 Carbon {args.nozzle_diameter} nozzle",
            "nickname": None,
            "nozzle_hrc": args.hrc,
            "nozzle_temperature": [args.temp_min, args.temp_max],
            "setting_id": setting_id,
            "update_time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0.0.0",
        }
        push(args.host, args.name, entry, args.nozzle_diameter, args.username)
    elif args.cmd == "remove":
        remove(args.host, args.name, args.nozzle_diameter, args.username)


if __name__ == "__main__":
    main()
