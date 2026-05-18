#!/usr/bin/env bash
# Install git pre-commit hook for config validation and staged secret scanning.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

cat > "$HOOK_PATH" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail

python3 scripts/validate_config.py

staged_files="$(git diff --cached --name-only --diff-filter=ACMR)"
if [ -n "$staged_files" ]; then
  if printf '%s\n' "$staged_files" \
    | xargs grep -l -E '(sk-[a-zA-Z0-9]{20,}|ANTHROPIC_API_KEY\s*=\s*["'\'']sk-)' 2>/dev/null; then
    echo "Pre-commit: possible API key detected in staged files. Remove it before committing."
    exit 1
  fi
fi

echo "Pre-commit: all checks passed."
HOOK

chmod +x "$HOOK_PATH"
echo "Pre-commit hook installed at $HOOK_PATH"
