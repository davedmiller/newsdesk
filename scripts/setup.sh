#!/bin/sh
# ABOUTME: Per-machine setup for newsdesk — creates symlink, adds to PATH, initializes config.
# ABOUTME: Run from the newsdesk repo directory after cloning.

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$HOME/bin"
LINK="$BIN_DIR/newsdesk"
ZSHRC="$HOME/.zshrc"

# Ensure ~/bin exists
mkdir -p "$BIN_DIR"

# Add ~/bin to PATH in .zshrc if not already there
if ! grep -q 'export PATH="\$HOME/bin:\$PATH"' "$ZSHRC" 2>/dev/null; then
    echo '' >> "$ZSHRC"
    echo '# newsdesk' >> "$ZSHRC"
    echo 'export PATH="$HOME/bin:$PATH"' >> "$ZSHRC"
    echo "Added ~/bin to PATH in $ZSHRC"
    export PATH="$BIN_DIR:$PATH"
else
    echo "~/bin already in PATH via $ZSHRC"
fi

# Ensure wrapper is executable
chmod +x "$REPO_DIR/newsdesk"

# Create or update symlink
if [ -L "$LINK" ]; then
    current="$(readlink "$LINK")"
    if [ "$current" = "$REPO_DIR/newsdesk" ]; then
        echo "Symlink already correct: $LINK -> $REPO_DIR/newsdesk"
    else
        ln -sf "$REPO_DIR/newsdesk" "$LINK"
        echo "Symlink updated: $LINK -> $REPO_DIR/newsdesk"
    fi
elif [ -e "$LINK" ]; then
    echo "ERROR: $LINK exists but is not a symlink. Remove it first."
    exit 1
else
    ln -s "$REPO_DIR/newsdesk" "$LINK"
    echo "Symlink created: $LINK -> $REPO_DIR/newsdesk"
fi

# Initialize config
"$LINK" init

MACHINE_NAME="$(hostname -s)"
"$LINK" send "Newsdesk Setup" "newsdesk successfully installed on $MACHINE_NAME" --priority 0 --project newsdesk

echo ""
echo "Done. Run 'source ~/.zshrc' or open a new terminal to use 'newsdesk'."
