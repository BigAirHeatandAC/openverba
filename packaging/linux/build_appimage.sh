#!/usr/bin/env bash
# ===========================================================================
# VoiceFlow - build a Linux AppImage from the PyInstaller onedir output.
#
# AppImage is the Linux PRIMARY artifact (PRODUCTION_PLAN.md sec 4.2): one
# portable, UNSANDBOXED file, so global hotkeys (evdev) and synthetic paste
# (ydotool/wtype/xdotool) actually work -- the Flatpak sandbox can break them.
#
# Pipeline:
#   1) pyinstaller packaging/voiceflow.spec  ->  dist/VoiceFlow/  (onedir)
#   2) this script lays out an AppDir around that onedir and runs appimagetool
#      ->  dist/VoiceFlow-x86_64.AppImage
#
# IMPORTANT: build on the OLDEST glibc you support (e.g. ubuntu-22.04, NOT
# ubuntu-latest) -- PyInstaller Linux binaries are glibc-forward-compatible only.
#
# Usage (from the project root, after the PyInstaller build):
#   bash packaging/linux/build_appimage.sh
#
# Env overrides:
#   ARCH        target arch label for the AppImage name (default: uname -m)
#   APP_VERSION version embedded in the filename (default: 1.0.0)
#   DIST_DIR    PyInstaller onedir output (default: dist/VoiceFlow)
#   OUT_DIR     where to write the .AppImage (default: dist)
# ===========================================================================
set -euo pipefail

# --- Resolve project root (this script lives in <root>/packaging/linux) -----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${ROOT}"

APP_NAME="VoiceFlow"
ARCH="${ARCH:-$(uname -m)}"
APP_VERSION="${APP_VERSION:-1.0.0}"
DIST_DIR="${DIST_DIR:-${ROOT}/dist/${APP_NAME}}"
OUT_DIR="${OUT_DIR:-${ROOT}/dist}"
APPDIR="${ROOT}/dist/${APP_NAME}.AppDir"
DESKTOP_SRC="${SCRIPT_DIR}/voiceflow.desktop"
ICON_PNG="${ROOT}/assets/voiceflow.png"

echo "============================================================"
echo " VoiceFlow AppImage build"
echo "   root    : ${ROOT}"
echo "   onedir  : ${DIST_DIR}"
echo "   arch    : ${ARCH}"
echo "   version : ${APP_VERSION}"
echo "============================================================"

# --- Preconditions ---------------------------------------------------------
if [[ ! -x "${DIST_DIR}/${APP_NAME}" && ! -f "${DIST_DIR}/${APP_NAME}" ]]; then
  echo "[ERROR] PyInstaller onedir not found at: ${DIST_DIR}/${APP_NAME}" >&2
  echo "        Run first:  pyinstaller --noconfirm --clean packaging/voiceflow.spec" >&2
  exit 1
fi

# --- Locate appimagetool (PATH, else download the AppImage of the tool) -----
APPIMAGETOOL="$(command -v appimagetool || true)"
if [[ -z "${APPIMAGETOOL}" ]]; then
  echo "[INFO] appimagetool not on PATH; downloading a pinned release..."
  TOOL_ARCH="${ARCH}"
  TOOL="${ROOT}/dist/appimagetool-${TOOL_ARCH}.AppImage"
  curl -fsSL -o "${TOOL}" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${TOOL_ARCH}.AppImage"
  chmod +x "${TOOL}"
  APPIMAGETOOL="${TOOL}"
fi

# --- Build the AppDir ------------------------------------------------------
echo "[1/4] Laying out AppDir at ${APPDIR}"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

# Copy the entire onedir runtime into usr/bin (keeps the _internal layout).
cp -a "${DIST_DIR}/." "${APPDIR}/usr/bin/"

# --- .desktop (top-level + the freedesktop location) -----------------------
echo "[2/4] Installing .desktop + icon"
cp "${DESKTOP_SRC}" "${APPDIR}/${APP_NAME}.desktop"
cp "${DESKTOP_SRC}" "${APPDIR}/usr/share/applications/${APP_NAME}.desktop"

# --- Icon: AppImage wants <iconname>.png at the AppDir root + hicolor -------
if [[ -f "${ICON_PNG}" ]]; then
  cp "${ICON_PNG}" "${APPDIR}/voiceflow.png"
  cp "${ICON_PNG}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/voiceflow.png"
else
  echo "[WARN] ${ICON_PNG} missing; AppImage will have no icon."
fi

# --- AppRun: entry the AppImage executes on launch -------------------------
# Exec the frozen binary, forwarding args. ydotool/wtype/xclip/wl-clipboard are
# resolved from the host PATH at runtime (NOT bundled) -- the app surfaces an
# in-app diagnostic if a needed tool is missing (PRODUCTION_PLAN.md sec 2.5).
echo "[3/4] Writing AppRun"
cat > "${APPDIR}/AppRun" <<'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
exec "${HERE}/usr/bin/VoiceFlow" "$@"
APPRUN
chmod +x "${APPDIR}/AppRun"

# --- Run appimagetool ------------------------------------------------------
echo "[4/4] Running appimagetool"
mkdir -p "${OUT_DIR}"
OUTPUT="${OUT_DIR}/${APP_NAME}-${APP_VERSION}-${ARCH}.AppImage"
# ARCH env tells appimagetool the target arch; --no-appstream avoids needing
# AppStream metadata for this app.
ARCH="${ARCH}" "${APPIMAGETOOL}" --no-appstream "${APPDIR}" "${OUTPUT}"

echo "============================================================"
echo " BUILD COMPLETE"
echo "   AppImage : ${OUTPUT}"
echo "============================================================"
