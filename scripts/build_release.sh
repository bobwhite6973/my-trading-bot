#!/usr/bin/env bash
#
# build_release.sh — Build the LeverBot release package.
#
# Usage:
#   ./scripts/build_release.sh [--version v1.0.0] [--mode pyarmor|pyc]
#
# Modes:
#   pyarmor  — Use PyArmor to obfuscate (requires paid PyArmor license for large files)
#   pyc      — Compile .py → .pyc as basic protection (free, no license needed)
#
# Output: dist/ directory with everything needed for Render deploy.
# Then commits and pushes to the 'release' branch.
#
set -euo pipefail

VERSION="${1:-v$(date +%Y.%m.%d)}"
MODE="${2:-pyarmor}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DIST_DIR="$ROOT_DIR/dist"
RELEASE_BRANCH="release"

cd "$ROOT_DIR"

echo "=== LeverBot Release Builder ==="
echo "Version: $VERSION"
echo "Mode:    $MODE"
echo ""

# ── Clean previous build ─────────────────────────────────────────────────────
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# ── Obfuscate / compile ──────────────────────────────────────────────────────
if [ "$MODE" = "pyarmor" ]; then
    echo "→ Running PyArmor obfuscation..."
    if ! command -v pyarmor &>/dev/null; then
        echo "ERROR: pyarmor not found. Install with: pip install pyarmor"
        echo "       A paid license is required for files > ~32KB."
        exit 1
    fi
    
    # Generate obfuscated files
    # --obf-code 2: advanced obfuscation (requires paid license for large files)
    # --mix-str: obfuscate string constants
    # --restrict: restrict mode — script only runs in obfuscated form
    pyarmor gen -O "$DIST_DIR" \
        --obf-code 2 \
        --mix-str \
        --restrict \
        main.py control_api.py
    
    echo "✓ PyArmor obfuscation complete"
    
elif [ "$MODE" = "pyc" ]; then
    echo "→ Compiling .py → .pyc (basic protection)..."
    
    # Compile source files to .pyc
    python3 -m compileall -b "$ROOT_DIR/main.py"
    python3 -m compileall -b "$ROOT_DIR/control_api.py"
    
    # Move compiled files to dist
    cp "$ROOT_DIR/main.pyc" "$DIST_DIR/main.pyc"
    cp "$ROOT_DIR/control_api.pyc" "$DIST_DIR/control_api.pyc"
    
    # Clean up .pyc files from source
    rm -f "$ROOT_DIR/main.pyc" "$ROOT_DIR/control_api.pyc"
    
    echo "✓ .pyc compilation complete"
    
else
    echo "ERROR: Unknown mode '$MODE'. Use 'pyarmor' or 'pyc'."
    exit 1
fi

# ── Copy supporting files ────────────────────────────────────────────────────
echo "→ Copying supporting files..."

# Always include these
cp "$ROOT_DIR/requirements.txt" "$DIST_DIR/"
cp "$ROOT_DIR/.env.example" "$DIST_DIR/"
cp "$ROOT_DIR/config.json" "$DIST_DIR/"
cp "$ROOT_DIR/generate_keys.py" "$DIST_DIR/"
cp "$ROOT_DIR/config_loader.py" "$DIST_DIR/"
cp "$ROOT_DIR/jupiter_fix.py" "$DIST_DIR/"

# Copy modules directory
cp -r "$ROOT_DIR/modules" "$DIST_DIR/modules"

# Create a fresh keys.json placeholder
cat > "$DIST_DIR/keys.json" << 'KEYS_EOF'
[
  {
    "key": "LB-TEST-TEST-TEST",
    "created": "2026-01-01T00:00:00+00:00",
    "expires": "2099-12-31T23:59:59+00:00",
    "type": "full"
  }
]
KEYS_EOF

# Copy render.yaml if it exists
if [ -f "$ROOT_DIR/render.yaml" ]; then
    cp "$ROOT_DIR/render.yaml" "$DIST_DIR/"
fi

echo "✓ Supporting files copied"

# ── Create release branch and push ────────────────────────────────────────────
echo ""
echo "→ Updating release branch..."

# Save current branch
CURRENT_BRANCH=$(git branch --show-current)

# Stash any uncommitted changes
git stash --quiet 2>/dev/null || true

# Create or update release branch
if git show-ref --verify --quiet "refs/heads/$RELEASE_BRANCH"; then
    git checkout "$RELEASE_BRANCH"
else
    git checkout --orphan "$RELEASE_BRANCH"
fi

# Remove old tracked files, keep dist/
git rm -rf --quiet . 2>/dev/null || true

# Copy dist contents to root of release branch
cp -r "$DIST_DIR"/* .
cp -r "$DIST_DIR"/.[!.]* . 2>/dev/null || true

# Add everything
git add -A

# Commit
git commit -m "release: $VERSION" --allow-empty

# Tag
git tag -f "$VERSION"

# Push
echo "→ Pushing release branch..."
git push -f origin "$RELEASE_BRANCH"
git push -f origin "$VERSION"

# Return to original branch
git checkout "$CURRENT_BRANCH"
git stash pop --quiet 2>/dev/null || true

echo ""
echo "=== Release $VERSION built and pushed ==="
echo "Release branch: $RELEASE_BRANCH"
echo "Tag:            $VERSION"
echo ""
echo "Deploy URL: https://render.com/deploy?repo=https://github.com/bobwhite6973/my-trading-bot&branch=$RELEASE_BRANCH"
