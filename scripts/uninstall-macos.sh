#!/usr/bin/env bash
# Remove the keylight-watch launchd agent and installed files. Safe to re-run.
source "$(dirname "$0")/_common.sh" "$@"

LABEL=local.keylight.watch
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "unloaded and removed $LABEL"
remove_package
