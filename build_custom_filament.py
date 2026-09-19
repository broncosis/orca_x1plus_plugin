#!/usr/bin/env python3
"""
build_custom_filament.py

Builds an X1Plus-compatible "filament database override" zip that you can
point the printer at via the `filament.filename` setting, to add filaments
that aren't in Bambu's official catalog (or to override existing entries).

Background / how this works:
  - bbl_screen normally loads its filament catalog from a Bambu-signed zip
    (ota-filament-*.zip.sig). That file is actually [528-byte custom header
    with an embedded signature] + [a completely ordinary zip].
  - X1Plus's interpose.cpp adds an `filament.filename` setting: if set to a
    path that exists on disk, that file is used INSTEAD of the official
    catalog for filament resource lookups.
  - Critically, the firmware's signature-verification hook
    (bbl_sal_verify_firmware_x2path) is intercepted by X1Plus and returns
    "verified OK" without actually checking anything, as long as the
    filename does NOT contain ".sig". So an override file just needs to be
    a plain, ordinary zip -- no signature, no 528-byte header required.
  - The zip contains one JSON file per nozzle diameter
    (filament-0.2.json / -0.4.json / -0.6.json / -0.8.json), each a flat
    dict keyed by the display name shown in the AMS picker:

        "My Custom PLA": {
          "type": "PLA",
          "filament_id": "GFL999",       # must not collide with an existing id
          "nozzle_temperature": [200, 230],
          "filament_is_support": "0",
          "required_nozzle_HRC": 3,
          "filament_vendor": "MyBrand",
          "setting_id": "GFSL999",
          "chamber_temperatures": "0",
          "temperature_vitrification": "45"
        }

Usage:
  1. Edit NEW_FILAMENTS below with your real filament(s).
  2. Point BASE_DIR at the four extracted filament-*.json files (the ones
     pulled out of your printer's real ota-filament-*.zip.sig).
  3. Run: python3 build_custom_filament.py
  4. Deploy the resulting custom-filament.zip (see deploy_custom_filament.sh).
"""

import json
import zipfile
import os
import sys

# ---------------------------------------------------------------------------
# EDIT ME: the filament(s) you want to add. Add as many keys as you like.
# The dict key is exactly what will show up in the AMS filament picker.
# ---------------------------------------------------------------------------
NEW_FILAMENTS = {
    "Bobcat3D Example PLA": {
        "type": "PLA",
        "filament_id": "GFL999",
        "nozzle_temperature": [200, 230],
        "filament_is_support": "0",
        "required_nozzle_HRC": 3,
        "filament_vendor": "Bobcat3D",
        "setting_id": "GFSL999",
        "chamber_temperatures": "0",
        "temperature_vitrification": "45",
    },
}

# Directory holding the four extracted+pretty-printed filament-*.json files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NOZZLE_FILES = ["filament-0.2.json", "filament-0.4.json", "filament-0.6.json", "filament-0.8.json"]
OUTPUT_ZIP = os.path.join(BASE_DIR, "custom-filament.zip")


def load_base(nozzle_file):
    path = os.path.join(BASE_DIR, nozzle_file)
    if not os.path.exists(path):
        print(f"WARNING: {nozzle_file} not found next to this script -- "
              f"starting from an empty catalog for that nozzle size. "
              f"This means only your custom entries will exist for that "
              f"nozzle diameter (existing Bambu/Generic materials will "
              f"disappear from the picker for that size). Put the real "
              f"extracted file next to this script to merge instead of "
              f"replace.", file=sys.stderr)
        return {}
    with open(path) as f:
        return json.load(f)


def check_collisions(catalog, new_entries, nozzle_file):
    existing_ids = {v["filament_id"] for v in catalog.values()}
    for name, entry in new_entries.items():
        if entry["filament_id"] in existing_ids and name not in catalog:
            print(f"ERROR: filament_id {entry['filament_id']!r} for "
                  f"{name!r} collides with an existing entry in "
                  f"{nozzle_file}. Pick a different filament_id/setting_id.",
                  file=sys.stderr)
            sys.exit(1)


def main():
    merged = {}
    for nozzle_file in NOZZLE_FILES:
        catalog = load_base(nozzle_file)
        check_collisions(catalog, NEW_FILAMENTS, nozzle_file)
        catalog.update(NEW_FILAMENTS)
        merged[nozzle_file] = catalog

    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for nozzle_file, catalog in merged.items():
            z.writestr(nozzle_file, json.dumps(catalog))

    print(f"Wrote {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP)} bytes)")
    print(f"Contains: {', '.join(merged.keys())}")
    print(f"New/overridden entries: {', '.join(NEW_FILAMENTS.keys())}")
    print()
    print("IMPORTANT: this filename must NOT contain '.sig' anywhere, or")
    print("X1Plus's verification bypass won't kick in and the real (failing)")
    print("Bambu signature check will run instead.")


if __name__ == "__main__":
    main()
