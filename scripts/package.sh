#!/usr/bin/env bash
# package.sh: builds the Pi release dist/dog-<version>.tar.gz and its .sha256 file, and prints the tarball path.
# The version is `git describe --tags --always --dirty`. The tarball holds the tracked files under pi/,
# hooks/, deploy/pi/ and config/ in name order, plus a VERSION file. It never holds tests or a venv.
# Owner, group and time stamps are fixed, so the same tree gives the same bytes.
set -euo pipefail

cd "$(dirname "$0")/.."
version=$(git describe --tags --always --dirty)
name="dog-$version.tar.gz"
mkdir -p dist
python3 - "dist/$name" "$version" <<'PY'
import gzip, io, subprocess, sys, tarfile

out, version = sys.argv[1], sys.argv[2]
listing = subprocess.run(["git", "ls-files", "-z", "--", "pi", "hooks", "deploy/pi", "config"],
                         check=True, capture_output=True).stdout.decode()
names = sorted(n for n in listing.split("\0") if n)
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
    for name in names:
        tar.add(name, recursive=False, filter=norm)
    data = (version + "\n").encode()
    info = norm(tarfile.TarInfo("VERSION"))
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
PY
if command -v sha256sum >/dev/null; then
	(cd dist && sha256sum "$name") >"dist/$name.sha256"
else
	(cd dist && shasum -a 256 "$name") >"dist/$name.sha256"
fi
echo "dist/$name"
