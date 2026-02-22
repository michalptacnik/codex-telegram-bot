#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: build_deb.sh [--version VERSION] [--output-dir DIR]

Builds an internal Debian package artifact for codex-telegram-bot.
The package contains project sources and operational scripts under /opt/codex-telegram-bot.
EOF
}

VERSION=""
OUTPUT_DIR="dist"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_NAME="codex-telegram-bot"
ARCH="$(dpkg --print-architecture)"
TS_UTC="$(date -u +%Y%m%d%H%M)"
GIT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")"

derive_version() {
  local base="0.1.0"
  if [[ -n "${GITHUB_REF_NAME:-}" && "${GITHUB_REF_TYPE:-}" == "tag" ]]; then
    local tag="${GITHUB_REF_NAME#v}"
    if [[ "$tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*$ ]]; then
      # Debian does not allow '-' in version segment; normalize to '~'
      tag="${tag//-/"~"}"
      echo "$tag"
      return 0
    fi
  fi
  echo "${base}~git${TS_UTC}.${GIT_SHA}"
}

validate_version() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9][0-9A-Za-z.+:~]*$ ]]; then
    echo "Invalid Debian version: $value" >&2
    exit 1
  fi
}

if [[ -z "$VERSION" ]]; then
  VERSION="$(derive_version)"
fi
validate_version "$VERSION"

OUT_ABS="$ROOT_DIR/$OUTPUT_DIR"
STAGE="$OUT_ABS/pkgroot"
DEBIAN_DIR="$STAGE/DEBIAN"
INSTALL_ROOT="$STAGE/opt/$PKG_NAME"

rm -rf "$STAGE"
mkdir -p "$DEBIAN_DIR" "$INSTALL_ROOT" "$OUT_ABS"

cat > "$DEBIAN_DIR/control" <<EOF
Package: $PKG_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.10), python3-venv, systemd
Maintainer: Michal Ptacnik
Description: Codex Telegram bot with control center and local runner
 Internal package for Ubuntu-first deployment and testing.
EOF

cat > "$DEBIAN_DIR/postinst" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
fi
EOF
chmod 0755 "$DEBIAN_DIR/postinst"

cat > "$DEBIAN_DIR/prerm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
fi
EOF
chmod 0755 "$DEBIAN_DIR/prerm"

rsync -a \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  --exclude 'dist' \
  "$ROOT_DIR/" "$INSTALL_ROOT/"

mkdir -p "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/codex-telegram-bootstrap" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec /opt/$PKG_NAME/scripts/bootstrap_ubuntu.sh "\$@"
EOF
chmod 0755 "$STAGE/usr/bin/codex-telegram-bootstrap"

DEB_FILE="$OUT_ABS/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --build "$STAGE" "$DEB_FILE" >/dev/null

SHA_FILE="$DEB_FILE.sha256"
(cd "$OUT_ABS" && sha256sum "$(basename "$DEB_FILE")" > "$(basename "$SHA_FILE")")

PROV_FILE="$OUT_ABS/${PKG_NAME}_${VERSION}_${ARCH}.provenance.json"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$PROV_FILE" <<EOF
{
  "package": "$PKG_NAME",
  "version": "$VERSION",
  "architecture": "$ARCH",
  "deb_file": "$(basename "$DEB_FILE")",
  "sha256_file": "$(basename "$SHA_FILE")",
  "source_commit": "$GIT_SHA",
  "build_time_utc": "$BUILD_TIME",
  "github": {
    "repository": "${GITHUB_REPOSITORY:-}",
    "run_id": "${GITHUB_RUN_ID:-}",
    "run_attempt": "${GITHUB_RUN_ATTEMPT:-}",
    "workflow": "${GITHUB_WORKFLOW:-}",
    "ref": "${GITHUB_REF:-}"
  }
}
EOF

rm -rf "$STAGE"

echo "Built package: $DEB_FILE"
echo "Checksum:      $SHA_FILE"
echo "Provenance:    $PROV_FILE"
