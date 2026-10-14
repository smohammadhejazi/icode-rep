#!/usr/bin/env bash
# Build what run.py and evaluate.py need; rerun after changing iCode's code.
#
#   runtime/            CPython (python-build-standalone, the version in iCode's
#                       .python-version) with iCode from this checkout and its
#                       locked dependencies installed; mounted read-only into the
#                       instance containers of glibc images
#   runtime-musl/       the same for the Alpine (musl) images
#   external/SWE-bench_Pro-os/   the official evaluation harness at a pinned commit
#   .venv/              this directory's own environment (uv sync)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ICODE="$(cd "$HERE/../.." && pwd)"
# SWE-bench_Pro-os v2.0.0: run scripts for all 731 V1 tasks.
PRO_OS_COMMIT=66f92766bba642462d4bbe5479e83f91f9211862
# Any musl image: the musl runtime is patched and byte-compiled inside it.
MUSL_BUILD_IMAGE="${SWEPRO_MUSL_BUILD_IMAGE:-alpine:3.20}"
# iCode patches some installed packages (textual, ...) the first time it starts;
# the runtimes are mounted read-only in the containers, so apply them at build time.
APPLY_PATCHES='
import sys
from chrys.foundation.patches import apply_all
errors = [r for r in apply_all() if r.status == "error"]
for r in errors:
    print(f"patch error: {r.patch.package}/{r.patch.module_file}: {r.detail}", file=sys.stderr)
sys.exit(1 if errors else 0)'

echo "== iCode runtime from $ICODE"
PY_VERSION="$(cat "$ICODE/.python-version")"
uv python install "$PY_VERSION"
PY_HOME="$(dirname "$(dirname "$(readlink -f "$(uv python find "$PY_VERSION")")")")"
rm -rf "$HERE/runtime"
mkdir -p "$HERE/runtime"
cp -a "$PY_HOME" "$HERE/runtime/python"
rm -f "$HERE/runtime/python/lib/python"*/EXTERNALLY-MANAGED
PY="$HERE/runtime/python/bin/python${PY_VERSION%.*}"
(cd "$ICODE" && uv export --frozen --no-dev --no-hashes --no-emit-project --quiet) > "$HERE/runtime/requirements.txt"
uv pip install --quiet --python "$PY" -r "$HERE/runtime/requirements.txt"
# iCode's vendored ripgrep (gitignored); its search tool uses it.
[ -x "$ICODE/src/chrys/foundation/vendor/ripgrep/rg" ] || "$ICODE/scripts/fetch_rg.sh"
uv pip install --quiet --python "$PY" --no-deps --reinstall "$ICODE"
"$PY" -I -c "$APPLY_PATCHES"
"$PY" -I -m compileall -q "$HERE/runtime/python/lib" > /dev/null || true
"$PY" -I - "$ICODE" > "$HERE/runtime/BUILD_INFO.json" <<'EOF'
import json, subprocess, sys
from chrys import __version__
git = lambda *a: subprocess.run(["git", "-C", sys.argv[1], *a], capture_output=True, text=True).stdout.strip()
print(json.dumps({"libc": "glibc", "icode_version": __version__, "python": sys.version.split()[0],
                  "icode_commit": git("rev-parse", "HEAD"),
                  "icode_dirty": bool(git("status", "--porcelain", "--untracked-files=no", "--", "src", "pyproject.toml", "uv.lock"))}, indent=2))
EOF
cat "$HERE/runtime/BUILD_INFO.json"

echo "== iCode runtime for Alpine (musl) images"
uv python install "cpython-$PY_VERSION-linux-x86_64-musl"
rm -rf "$HERE/runtime-musl"
mkdir -p "$HERE/runtime-musl"
cp -a "$(uv python dir)/cpython-$PY_VERSION-linux-x86_64-musl" "$HERE/runtime-musl/python"
rm -f "$HERE/runtime-musl/python/lib/python"*/EXTERNALLY-MANAGED
cp "$HERE/runtime/requirements.txt" "$HERE/runtime-musl/requirements.txt"
SITE="$HERE/runtime-musl/python/lib/python${PY_VERSION%.*}/site-packages"
MUSL=(--python-platform x86_64-unknown-linux-musl --python-version "${PY_VERSION%.*}" --target "$SITE")
# musllinux wheels; a package with none is built from source here, which is
# only right for pure-Python code (watchdog 6.0.0): the check below fails the
# build if any native library links against glibc.
uv pip install --quiet "${MUSL[@]}" -r "$HERE/runtime-musl/requirements.txt"
uv pip install --quiet "${MUSL[@]}" --no-deps "$ICODE"
if find "$SITE" -name '*.so*' -type f -exec readelf -d {} \; 2>/dev/null | grep -q 'libc\.so\.6'; then
  echo "error: a native library in runtime-musl links against glibc" >&2
  exit 1
fi
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$HERE/runtime-musl:/opt/icode" \
  --entrypoint /bin/sh "$MUSL_BUILD_IMAGE" -c '
    set -e
    PY=/opt/icode/python/bin/python'"${PY_VERSION%.*}"'
    "$PY" -I -c "import pydantic_core, jiter, rpds, psutil, PIL.Image, yaml, watchdog.observers"
    "$PY" -I -c "$1"
    "$PY" -I -m compileall -q /opt/icode/python/lib > /dev/null || true
    "$PY" -I -c "from chrys import __version__; print(__version__)" > /opt/icode/.icode_version' \
  sh "$APPLY_PATCHES"
python3 - "$HERE" <<'EOF'
import json, pathlib, sys
here = pathlib.Path(sys.argv[1])
info = json.loads((here / "runtime/BUILD_INFO.json").read_text())
version = (here / "runtime-musl/.icode_version").read_text().strip()
info.update(libc="musl", icode_version=version)
(here / "runtime-musl/BUILD_INFO.json").write_text(json.dumps(info, indent=2) + "\n")
print(json.dumps(info, indent=2))
EOF

echo "== SWE-bench_Pro-os $PRO_OS_COMMIT"
if [ ! -d "$HERE/external/SWE-bench_Pro-os/.git" ]; then
  mkdir -p "$HERE/external"
  git clone --quiet https://github.com/scaleapi/SWE-bench_Pro-os.git "$HERE/external/SWE-bench_Pro-os"
fi
git -C "$HERE/external/SWE-bench_Pro-os" checkout --quiet "$PRO_OS_COMMIT"

echo "== harness environment"
(cd "$HERE" && uv sync --quiet)
echo "Done."
