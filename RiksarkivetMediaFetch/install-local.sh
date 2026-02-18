#!/usr/bin/env sh
set -eu

ADDON_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ADDON_NAME="RiksarkivetMediaFetch"

copy_to_dest() {
  base="$1"
  dest="$base/plugins/$ADDON_NAME"
  mkdir -p "$dest"
  cp \
    "$ADDON_DIR/RiksarkivetMediaFetch.gpr.py" \
    "$ADDON_DIR/RiksarkivetMediaFetch.py" \
    "$ADDON_DIR/README.md" \
    "$dest/"
  printf 'Installed %s to %s\n' "$ADDON_NAME" "$dest"
}

copy_unique() {
  candidate="$1"
  case " $seen_bases " in
    *" $candidate "*)
      return 0
      ;;
  esac
  seen_bases="$seen_bases $candidate"
  copy_to_dest "$candidate"
}

found=0
seen_bases=""

if [ -n "${GRAMPS_PLUGIN_BASE:-}" ]; then
  mkdir -p "$GRAMPS_PLUGIN_BASE"
  copy_unique "$GRAMPS_PLUGIN_BASE"
  exit 0
fi

# Standard host location.
if [ -d "$HOME/.local/share/gramps/gramps60" ]; then
  copy_unique "$HOME/.local/share/gramps/gramps60"
  found=1
fi

# Current XDG location (if different from standard).
if [ -n "${XDG_DATA_HOME:-}" ] &&
   [ "$XDG_DATA_HOME" != "$HOME/.local/share" ] &&
   [ -d "$XDG_DATA_HOME/gramps/gramps60" ]; then
  copy_unique "$XDG_DATA_HOME/gramps/gramps60"
  found=1
fi

# Flatpak-style locations (all apps, including VS Code Flatpak shell env).
if [ -d "$HOME/.var/app" ]; then
  for base in "$HOME"/.var/app/*/data/gramps/gramps60; do
    if [ -d "$base" ]; then
      copy_unique "$base"
      found=1
    fi
  done
fi

# If nothing exists yet, create standard path.
if [ "$found" -eq 0 ]; then
  mkdir -p "$HOME/.local/share/gramps/gramps60"
  copy_unique "$HOME/.local/share/gramps/gramps60"
fi
