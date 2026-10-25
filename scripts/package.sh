#!/usr/bin/env bash
# package.sh: builds the Pi release dist/dog-<version>.tar.gz and its .sha256 file, and prints the tarball path.
# The version is `git describe --tags --always --dirty`. The tarball holds the tracked files under pi/, hooks/,
# deploy/pi/ and config/ in name order, plus VERSION. Fixed owners, times and modes make it reproducible.
set -euo pipefail

cd "$(dirname "$0")/.."
version=$(git describe --tags --always --dirty)
mkdir -p dist
python3 - "dist/dog-$version.tar.gz" "$version" <<'PY'
import gzip, hashlib, io, os, subprocess, sys, tarfile

out, version = sys.argv[1], sys.argv[2]
listing = subprocess.run(["git", "ls-files", "-z", "--", "pi", "hooks", "deploy/pi", "config"],
                         check=True, capture_output=True).stdout.decode()
mtime = int(subprocess.run(["git", "log", "-1", "--format=%ct"], check=True, capture_output=True).stdout)


def norm(info):
    """Give a member fixed owner, time and mode bits, so the tarball is reproducible."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = mtime
    info.mode = 0o755 if info.mode & 0o100 else 0o644
    return info


with open(out, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
    for name in sorted(filter(None, listing.split("\0"))):
        tar.add(name, recursive=False, filter=norm)
    data = (version + "\n").encode()
    info = norm(tarfile.TarInfo("VERSION"))
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
with open(out, "rb") as handle, open(out + ".sha256", "w") as digest:
    digest.write(f"{hashlib.sha256(handle.read()).hexdigest()}  {os.path.basename(out)}\n")
print(out)
PY
