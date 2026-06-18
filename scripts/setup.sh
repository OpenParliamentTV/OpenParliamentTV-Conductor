#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$BASE_DIR"

# Tools repo URL is the only hardcoded URL — it's shared across all parliaments,
# so it doesn't belong in per-parliament config.
TOOLS_REPO="https://github.com/OpenParliamentTV/OpenParliamentTV-Tools.git"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

success() { echo -e "${GREEN}✓${NC} $1"; }
warning() { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }

# set_env KEY VALUE — set KEY=VALUE in config/secrets.env, replacing an existing
# line or appending if absent. Portable across BSD (macOS) and GNU sed.
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" config/secrets.env 2>/dev/null; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" config/secrets.env
        else
            sed -i "s|^${key}=.*|${key}=${value}|" config/secrets.env
        fi
    else
        echo "${key}=${value}" >> config/secrets.env
    fi
}

echo "========================================"
echo "OpenParliamentTV-Conductor Setup"
echo "========================================"
echo ""

# Check prerequisites
echo "Checking prerequisites..."

command -v docker &>/dev/null || error "Docker not installed"
success "Docker installed"

command -v git &>/dev/null || error "Git not installed"
success "Git installed"

command -v python3 &>/dev/null || error "Python 3 not installed"
success "Python 3 installed"

command -v ssh-keygen &>/dev/null || error "ssh-keygen not installed"

# Create directories
echo ""
echo "Creating directories..."
mkdir -p data status logs nginx/ssl config/ssh
chmod 700 config/ssh
success "Directories created"

# Clone Tools repo (HTTPS — no SSH needed)
echo ""
echo "Setting up Tools repository..."
if [ ! -d "data/OpenParliamentTV-Tools" ]; then
    echo "Cloning OpenParliamentTV-Tools..."
    git clone "$TOOLS_REPO" data/OpenParliamentTV-Tools
    success "Tools repository cloned"
else
    success "Tools repository exists"
fi

# Create config files from samples
echo ""
echo "Creating configuration files..."

copy_sample() {
    if [ -f "$2" ]; then
        warning "$2 already exists (skipping)"
    else
        cp "$1" "$2"
        success "Created $2"
    fi
}

copy_sample config/secrets.env.sample config/secrets.env
copy_sample config/parliaments.yaml.sample config/parliaments.yaml
copy_sample config/users.yaml.sample config/users.yaml
copy_sample config/schedules.yaml.sample config/schedules.yaml
copy_sample config/notifications.yaml.sample config/notifications.yaml

# GitHub authentication: enabled by default. Declining bypasses login entirely —
# every request gets admin access, and the OAuth/JWT/users.yaml steps are skipped.
echo ""
AUTH_ENABLED=true
read -r -p "Enable GitHub authentication? [Y/n] " _auth_reply
case "$_auth_reply" in
    [Nn]*) AUTH_ENABLED=false ;;
esac
if [ "$AUTH_ENABLED" = false ]; then
    set_env AUTH_ENABLED false
    warning "GitHub auth DISABLED — anyone who can reach this server has admin access"
else
    set_env AUTH_ENABLED true
    success "GitHub auth enabled"
fi

# Generate JWT secret if empty (only needed when auth is enabled)
if [ "$AUTH_ENABLED" = true ] && grep -q "^JWT_SECRET=$" config/secrets.env 2>/dev/null; then
    JWT_SECRET=$(openssl rand -hex 32)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/^JWT_SECRET=$/JWT_SECRET=$JWT_SECRET/" config/secrets.env
    else
        sed -i "s/^JWT_SECRET=$/JWT_SECRET=$JWT_SECRET/" config/secrets.env
    fi
    success "Generated JWT_SECRET"
fi

chmod 600 config/secrets.env
success "Set secure permissions on secrets.env"

# Generate dedicated SSH key for git operations
echo ""
echo "Setting up dedicated SSH key for git operations..."
if [ ! -f config/ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f config/ssh/id_ed25519 -N "" -C "optv-conductor@$(hostname)" >/dev/null
    chmod 600 config/ssh/id_ed25519
    success "Generated dedicated Conductor SSH key (config/ssh/id_ed25519)"
else
    success "SSH key exists (config/ssh/id_ed25519)"
fi
if [ ! -f config/ssh/known_hosts ]; then
    ssh-keyscan -t ed25519 github.com >> config/ssh/known_hosts 2>/dev/null
    success "Pinned github.com host key"
else
    success "known_hosts exists"
fi

# Iterate over enabled parliaments — read git_remote + entity_dump_url from
# Conductor config + Tools manifest, clone Data repos and download entity dumps.
echo ""
echo "Setting up enabled parliaments..."

# Emits one TSV line per enabled parliament: <id>\t<git_remote>\t<entity_dump_url>
read_parliaments() {
    PYTHONPATH="data/OpenParliamentTV-Tools" python3 - <<'PY'
import sys
import yaml

try:
    with open("config/parliaments.yaml") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    sys.exit(0)

parliaments = (cfg.get("parliaments") or {})
try:
    from optv.parliaments import load_manifest
except ImportError:
    load_manifest = None

for pid, p in parliaments.items():
    p = p or {}
    if not p.get("enabled", True):
        continue
    git_remote = p.get("git_remote", "")
    entity_url = p.get("entity_dump_url")
    if entity_url is None and load_manifest is not None:
        try:
            entity_url = load_manifest(pid).get("entity_dump_url", "")
        except Exception:
            entity_url = ""
    print(f"{pid}\t{git_remote}\t{entity_url or ''}")
PY
}

while IFS=$'\t' read -r pid git_remote entity_url; do
    [ -z "$pid" ] && continue
    data_dir="data/OpenParliamentTV-Data-${pid}"
    if [ ! -d "$data_dir" ]; then
        if [ -z "$git_remote" ]; then
            warning "$pid: no git_remote set in parliaments.yaml — skipping clone"
        else
            echo "Cloning $git_remote → $data_dir"
            if GIT_SSH_COMMAND="ssh -i config/ssh/id_ed25519 -o UserKnownHostsFile=config/ssh/known_hosts -o IdentitiesOnly=yes" \
                git clone "$git_remote" "$data_dir"; then
                success "$pid: cloned"
            else
                warning "$pid: clone failed — add the deploy key (see below) and re-run setup.sh"
                continue
            fi
        fi
    else
        success "$pid: data directory exists"
    fi

    if [ -n "$entity_url" ] && [ -d "$data_dir" ]; then
        mkdir -p "$data_dir/metadata"
        if curl -sf -o "$data_dir/metadata/entities.json" "$entity_url"; then
            success "$pid: entity dump downloaded"
        else
            warning "$pid: entity dump download failed (optional)"
        fi
    fi
done < <(read_parliaments)

# Summary
echo ""
echo "========================================"
echo "Setup Complete!"
echo "========================================"
echo ""
echo "Add this public key as a deploy key (with WRITE access) on each Data repo:"
echo ""
cat config/ssh/id_ed25519.pub
echo ""
echo "Deploy-key URLs (one per parliament's Data repo):"
PYTHONPATH="data/OpenParliamentTV-Tools" python3 - <<'PY' 2>/dev/null || true
import re
import yaml
try:
    with open("config/parliaments.yaml") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    raise SystemExit
for pid, p in (cfg.get("parliaments") or {}).items():
    p = p or {}
    if not p.get("enabled", True):
        continue
    remote = p.get("git_remote", "")
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", remote) or re.match(r"https://github\.com/(.+?)(?:\.git)?$", remote)
    if m:
        print(f"  https://github.com/{m.group(1)}/settings/keys")
PY
echo ""
echo "Next steps:"
echo ""
_step=1
if [ "$AUTH_ENABLED" = true ]; then
    echo "$_step. Create GitHub OAuth App:"
    echo "   https://github.com/settings/developers"
    echo "   Callback: http://localhost:8000/auth/callback"
    echo ""
    _step=$((_step + 1))
fi
echo "$_step. Edit config/secrets.env:"
if [ "$AUTH_ENABLED" = true ]; then
    echo "   - GITHUB_CLIENT_ID"
    echo "   - GITHUB_CLIENT_SECRET"
fi
echo "   - BASE_URL"
echo "   - GIT_USER_NAME / GIT_USER_EMAIL (publish-step git identity)"
echo ""
_step=$((_step + 1))
if [ "$AUTH_ENABLED" = true ]; then
    echo "$_step. Edit config/users.yaml:"
    echo "   Add your GitHub username"
    echo ""
    _step=$((_step + 1))
else
    warning "Auth is DISABLED — the UI requires no login and every visitor has admin access."
    echo ""
fi
echo "$_step. Add the deploy key shown above to each Data repo, then re-run"
echo "   ./scripts/setup.sh if any Data clones failed."
echo ""
_step=$((_step + 1))
echo "$_step. Start the application:"
echo "   docker compose up -d"
echo ""

# Warnings for missing config
if [ "$AUTH_ENABLED" = true ]; then
    grep -q "^GITHUB_CLIENT_ID=$" config/secrets.env && warning "Configure GITHUB_CLIENT_ID"
    grep -q "^GITHUB_CLIENT_SECRET=$" config/secrets.env && warning "Configure GITHUB_CLIENT_SECRET"
fi
grep -q "^BASE_URL=$" config/secrets.env && warning "Configure BASE_URL"
