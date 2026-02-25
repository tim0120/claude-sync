#!/bin/bash
# Claude Code hook: Sync conversations on session end
# Add to ~/.claude/settings.json under hooks.Stop

LOG_FILE="$HOME/.claude-sync/sync.log"

# Find sync.py in order of preference (persistent paths first, Mac fallback last)
SYNC_SCRIPT=""
for candidate in \
    "/pollux/timkostolansky/claude-sync/sync.py" \
    "/data/timkostolansky/claude-sync/sync.py" \
    "$HOME/Developer/claude-sync/sync.py"; do
    if [[ -f "$candidate" ]]; then
        SYNC_SCRIPT="$candidate"
        break
    fi
done

if [[ -z "$SYNC_SCRIPT" ]]; then
    echo "$(date -Iseconds) ERROR: sync.py not found in any known location" >> "$LOG_FILE"
    exit 0
fi

# Run sync in background to not block Claude exit
(
    echo "$(date -Iseconds) Starting sync (script: $SYNC_SCRIPT)..." >> "$LOG_FILE"
    python3 "$SYNC_SCRIPT" --push >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    if [[ $EXIT_CODE -ne 0 ]]; then
        echo "$(date -Iseconds) ERROR: sync exited with code $EXIT_CODE" >> "$LOG_FILE"
        # Notify user of failure
        if [[ "$(uname)" == "Darwin" ]]; then
            osascript -e 'display notification "claude-sync failed — check ~/.claude-sync/sync.log" with title "claude-sync"' 2>/dev/null || true
        elif command -v notify-send &>/dev/null; then
            notify-send "claude-sync" "Sync failed — check ~/.claude-sync/sync.log" 2>/dev/null || true
        fi
    else
        echo "$(date -Iseconds) Sync complete" >> "$LOG_FILE"
    fi
) &
