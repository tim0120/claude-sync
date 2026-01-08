# claude-sync

Sync Claude Code conversations to a git repo with machine metadata. Designed to work across SSH sessions and multiple machines.

## Features

- Syncs conversations from `~/.claude/projects/` to a local git repo
- Enriches with machine metadata (hostname, git remote, branch, model, etc.)
- Strips extended thinking blocks to reduce size (configurable)
- Auto-syncs on Claude Code session end via hook
- Organizes by machine and date for easy querying

## Quick Start

```bash
# Initialize (creates ~/.claude-sync/config.json and ~/.claude-sync/repo/)
python3 sync.py --init

# Check status
python3 sync.py --status

# Sync all new sessions
python3 sync.py

# Sync and push to remote
python3 sync.py --push
```

## Configuration

Config file: `~/.claude-sync/config.json`

```json
{
  "machine_id": "my-laptop",
  "sync_repo_path": "/path/to/sync/repo",
  "claude_projects_path": "~/.claude/projects",
  "include_thinking": false
}
```

## Setting Up Remote Sync

1. Create a private git repo (e.g., on GitHub)
2. Initialize with remote:
   ```bash
   python3 sync.py --init --remote git@github.com:you/claude-sync-data.git
   ```
3. Or add remote manually:
   ```bash
   cd ~/.claude-sync/repo
   git remote add origin git@github.com:you/claude-sync-data.git
   ```

## Auto-Sync Hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/claude-sync-hook.sh"
          }
        ]
      }
    ]
  }
}
```

## Repo Structure

```
~/.claude-sync/repo/
├── sessions/
│   └── <machine-id>/
│       └── <date>/
│           └── <session-id>.jsonl
├── metadata/
│   └── <machine-id>/
│       └── <session-id>.json
└── README.md
```

## Metadata Schema

Each session gets a metadata file with:

```json
{
  "session_id": "abc123",
  "machine_id": "my-laptop",
  "original_path": "/Users/me/projects/foo",
  "cwd": "/Users/me/projects/foo",
  "git_remote": "git@github.com:org/repo.git",
  "git_branch": "main",
  "model": "claude-opus-4-5-20251101",
  "message_count": 42,
  "started_at": "2025-01-07T10:00:00Z",
  "ended_at": "2025-01-07T11:30:00Z",
  "duration_seconds": 5400,
  "synced_at": "2025-01-07T12:00:00Z"
}
```

## Integration with claude-history MCP

This tool is designed to work alongside the [claude-history MCP](https://github.com/...) for searching conversations. The sync repo can be used as an alternative data source for indexing conversations across machines.

## Multi-Machine Setup

On each machine:

1. Clone this repo or copy `sync.py`
2. Run `python3 sync.py --init`
3. Edit `~/.claude-sync/config.json` to set a unique `machine_id`
4. Add the same git remote on all machines
5. Pull before syncing, push after: `git pull && python3 sync.py --push`

## License

MIT
