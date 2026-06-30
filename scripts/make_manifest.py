#!/usr/bin/env python3
"""make_manifest.py - emit website/latest.json for the auto-updater.

Writes the update manifest that already-installed OpenVerba apps poll at
https://openverba.com/latest.json. The schema matches voiceflow.updater
(version, url, sha256, size, notes, mandatory, min_version, pub_date).

Used by scripts/build.bat after the installer is built, e.g.:

    python scripts/make_manifest.py ^
        --version 1.1.0 ^
        --sha256 <64-hex> ^
        --exe dist/OpenVerba-Setup-1.1.0.exe ^
        --out website/latest.json

If --sha256 is omitted it is computed from --exe. --notes / --mandatory /
--min-version are optional. The download URL is derived from the version so it
always matches installer.iss's OutputBaseFilename (OpenVerba-Setup-<ver>.exe).
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

DOWNLOAD_BASE = "https://openverba.com/download"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Emit website/latest.json")
    ap.add_argument("--version", required=True, help="SemVer, no leading v")
    ap.add_argument("--exe", required=True, help="Path to the built installer")
    ap.add_argument("--sha256", default="", help="Override (else computed)")
    ap.add_argument("--out", required=True, help="Output JSON path")
    ap.add_argument("--notes", default="", help="Release notes")
    ap.add_argument("--mandatory", action="store_true")
    ap.add_argument("--min-version", default="", dest="min_version")
    ap.add_argument("--url", default="", help="Override download URL")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.exe):
        print("make_manifest: installer not found: %s" % args.exe, file=sys.stderr)
        return 2

    sha = (args.sha256 or "").strip().lower()
    if len(sha) != 64:
        sha = _sha256(args.exe)

    url = args.url.strip() or (
        "%s/OpenVerba-Setup-%s.exe" % (DOWNLOAD_BASE, args.version))

    manifest = {
        "version": args.version.strip(),
        "url": url,
        "sha256": sha,
        "size": os.path.getsize(args.exe),
        "notes": args.notes,
        "mandatory": bool(args.mandatory),
        "pub_date": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if args.min_version.strip():
        manifest["min_version"] = args.min_version.strip()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print("make_manifest: wrote %s (version=%s, %d bytes, sha=%s…)"
          % (args.out, manifest["version"], manifest["size"], sha[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
