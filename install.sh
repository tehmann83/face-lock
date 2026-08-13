#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# FaceGuard installer — macOS
# Installs all dependencies needed to run faceguard.py.
# Everything is free and runs 100% locally.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # no colour

info()    { echo -e "${GREEN}▶${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
success() { echo -e "${GREEN}✅ $*${NC}"; }
fail()    { echo -e "${RED}✗  $*${NC}"; exit 1; }

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   FaceGuard — Dependency Installer   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    info "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
else
    info "Homebrew already installed — skipping."
fi

# ── 2. cmake (required to compile dlib) ──────────────────────────────────────
if ! brew list cmake &>/dev/null; then
    info "Installing cmake via Homebrew..."
    brew install cmake
else
    info "cmake already installed — skipping."
fi

# ── 3. Python 3 ───────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    info "Installing Python 3 via Homebrew..."
    brew install python
else
    PY_VER=$(python3 --version 2>&1)
    info "Python already installed: ${PY_VER}"
fi

# ── 4. pip packages ───────────────────────────────────────────────────────────
info "Installing Python packages (this may take a few minutes — dlib compiles from source)..."

# Upgrade pip silently
python3 -m pip install --upgrade pip --quiet

# Install packages
python3 -m pip install \
    opencv-python \
    face_recognition \
    numpy \
    --quiet

success "All dependencies installed!"

# ── 5. Camera permission reminder ─────────────────────────────────────────────
echo ""
warn "Camera permission: the first time you run faceguard.py, macOS may ask"
warn "for camera access. Click OK — the app never sends video anywhere."
echo ""

# ── 6. Quick sanity check ─────────────────────────────────────────────────────
info "Verifying imports..."
python3 - <<'EOF'
import cv2
import face_recognition
import numpy
print(f"  opencv-python    {cv2.__version__}")
print(f"  face_recognition {face_recognition.__version__}")
print(f"  numpy            {numpy.__version__}")
EOF

# ── 7. Bootstrap .env ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
    info "Created .env from .env.example — edit it to enable Telegram alerts."
else
    info ".env already exists — skipping."
fi

echo ""
success "Installation complete!"
echo ""
echo "  Next steps:"
echo "    1.  (optional) edit .env to add Telegram credentials"
echo "    2.  python3 faceguard.py --enroll     ← teach it your face"
echo "    3.  python3 faceguard.py               ← start guarding"
echo ""
