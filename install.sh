#!/usr/bin/env bash
# Symlink the handoff skill into ~/.claude/skills so Claude Code picks it up.
# Re-running is safe; an existing real directory is backed up first.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
src="$here/skills/handoff"
dst="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/handoff"

mkdir -p "$(dirname "$dst")"
chmod +x "$src/scripts/"*

if [[ -L "$dst" ]]; then
  rm "$dst"
elif [[ -d "$dst" ]]; then
  backup="$dst.bak.$(date +%s)"
  echo "existing directory moved to $backup"
  mv "$dst" "$backup"
fi

ln -s "$src" "$dst"
echo "linked $dst -> $src"

for tool in python3; do
  command -v "$tool" >/dev/null || echo "warning: $tool not found; the skill needs it"
done
echo "start a new Claude Code session and type /handoff"
