#!/bin/sh
# ABOUTME: Installs the newsdesk relay as a LaunchAgent on the hub machine.
# ABOUTME: Safe to re-run: an already-loaded agent is unloaded first, then loaded from the fresh plist.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.dave.newsdesk-relay"
TEMPLATE="$REPO_DIR/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ ! -x "$HOME/bin/newsdesk" ]; then
    echo "ERROR: $HOME/bin/newsdesk not found. Run scripts/setup.sh first."
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/share/newsdesk"
sed "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
    # bootout returns before the job is gone; bootstrap fails if it is still unloading
    tries=0
    while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && [ "$tries" -lt 20 ]; do
        sleep 0.5
        tries=$((tries + 1))
    done
    echo "Unloaded the running $LABEL"
fi
launchctl bootstrap "$DOMAIN" "$TARGET"
echo "Loaded $LABEL"
echo "Log: $HOME/.local/share/newsdesk/relay.log"
