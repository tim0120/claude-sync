#!/usr/bin/env python3
"""
claude-sync: Sync Claude Code conversations to a git repo with rich metadata.

Usage:
    python sync.py                    # Sync all new conversations
    python sync.py --init             # Initialize config
    python sync.py --status           # Show sync status
"""

import json
import os
import platform
import subprocess
import socket
import getpass
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set
import argparse


# === Configuration ===

def get_config_path() -> Path:
    return Path.home() / ".claude-sync" / "config.json"


def get_default_config() -> dict:
    return {
        "machine_id": socket.gethostname(),
        "sync_repo_path": str(Path.home() / ".claude-sync" / "repo"),
        "claude_projects_path": str(Path.home() / ".claude" / "projects"),
        "sync_on_save": True,
        "include_thinking": False,  # Extended thinking blocks can be large
    }


def load_config() -> dict:
    config_path = get_config_path()
    if config_path.exists():
        with open(config_path) as f:
            return {**get_default_config(), **json.load(f)}
    return get_default_config()


def save_config(config: dict):
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"Config saved to {config_path}")


# === Git Helpers ===

def run_git(repo_path: Path, *args) -> subprocess.CompletedProcess:
    """Run a git command in the specified repo."""
    return subprocess.run(
        ["git", "-C", str(repo_path)] + list(args),
        capture_output=True,
        text=True,
    )


def get_git_remote(path: Path) -> Optional[str]:
    """Get the git remote URL for a path, if it's in a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return None


def get_git_branch(path: Path) -> Optional[str]:
    """Get current git branch for a path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except:
        pass
    return None


def get_git_commit(path: Path) -> Optional[str]:
    """Get current git commit hash for a path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]  # Short hash
    except:
        pass
    return None


def get_git_dirty(path: Path) -> Optional[bool]:
    """Check if git repo has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return len(result.stdout.strip()) > 0
    except:
        pass
    return None


def init_sync_repo(repo_path: Path, remote_url: Optional[str] = None):
    """Initialize the sync repo."""
    repo_path.mkdir(parents=True, exist_ok=True)

    if not (repo_path / ".git").exists():
        run_git(repo_path, "init")

        # Create initial structure (flat)
        (repo_path / "sessions").mkdir(exist_ok=True)
        (repo_path / "metadata").mkdir(exist_ok=True)

        # Create README
        readme = repo_path / "README.md"
        readme.write_text("""# Claude Sync Repository

This repository contains synced Claude Code conversations from multiple machines.

## Structure

```
sessions/
  <date>/
    <session-id>.jsonl      # Raw conversation (thinking stripped)
metadata/
  <session-id>.json         # Rich metadata (machine, git, tokens, etc.)
```

## Querying

```bash
# Find sessions from a specific machine
grep -l '"machine_id": "my-laptop"' metadata/*.json

# Find sessions for a specific git repo
grep -l '"git_remote": ".*myrepo.*"' metadata/*.json

# Sessions with most tool calls
jq -s 'sort_by(.tool_calls_count) | reverse | .[0:10] | .[] | {id: .session_id, tools: .tool_calls_count}' metadata/*.json
```

## Metadata Fields

Each session includes:
- Machine info: machine_id, hostname, username, platform
- Git context: remote, branch, commit, dirty status
- Session stats: duration, message_count, token usage
- Tool usage: tool_calls_count, tools_used, files_modified
- Timestamps: started_at, ended_at, synced_at
""")

        run_git(repo_path, "add", ".")
        run_git(repo_path, "commit", "-m", "Initial commit")

        if remote_url:
            run_git(repo_path, "remote", "add", "origin", remote_url)

        print(f"Initialized sync repo at {repo_path}")
    else:
        print(f"Sync repo already exists at {repo_path}")


# === Metadata Extraction ===

def extract_project_name(path: str) -> str:
    """Extract a human-readable project name from a path."""
    if not path:
        return "unknown"
    # Get the last meaningful directory component
    parts = [p for p in path.split("/") if p and p not in ("Users", "home", "root")]
    if parts:
        return parts[-1]
    return "unknown"


def extract_session_metadata(session_path: Path, config: dict) -> dict:
    """Extract rich metadata from a session file."""

    # Parse the project path from the directory name
    # Format: -Users-timkostolansky-Developer-workshop-labs-timbox
    project_dir = session_path.parent.name
    # Remove leading dash, then replace remaining dashes with slashes
    original_path = "/" + project_dir.lstrip("-").replace("-", "/")
    project_name = extract_project_name(original_path)

    # Initialize counters and collectors
    first_msg = None
    last_msg = None
    message_count = 0
    user_message_count = 0
    assistant_message_count = 0
    git_branch = None
    cwd = None
    model_used = None
    claude_version = None

    # Token tracking
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_creation_tokens = 0

    # Tool tracking
    tool_calls_count = 0
    tools_used: Set[str] = set()
    files_modified: Set[str] = set()

    # Session type detection
    session_id = session_path.stem
    is_agent_session = session_id.startswith("agent-")
    parent_session_id = None

    with open(session_path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                message_count += 1

                if first_msg is None:
                    first_msg = msg
                last_msg = msg

                # Extract basic info from any message
                if "gitBranch" in msg and not git_branch:
                    git_branch = msg["gitBranch"]
                if "cwd" in msg and not cwd:
                    cwd = msg["cwd"]
                if "version" in msg and not claude_version:
                    claude_version = msg["version"]

                # Count by type
                msg_type = msg.get("type", "")
                if msg_type == "user":
                    user_message_count += 1
                elif msg_type == "assistant":
                    assistant_message_count += 1

                    # Extract model and token usage
                    if "message" in msg:
                        message_data = msg["message"]
                        if "model" in message_data:
                            model_used = message_data["model"]

                        # Token usage
                        usage = message_data.get("usage", {})
                        total_input_tokens += usage.get("input_tokens", 0)
                        total_output_tokens += usage.get("output_tokens", 0)
                        total_cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                        total_cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

                        # Tool calls in content
                        content = message_data.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    tool_calls_count += 1
                                    tool_name = item.get("name", "unknown")
                                    tools_used.add(tool_name)

                                    # Track files modified by Edit/Write tools
                                    if tool_name in ("Edit", "Write", "NotebookEdit"):
                                        tool_input = item.get("input", {})
                                        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                                        if file_path:
                                            files_modified.add(file_path)

            except json.JSONDecodeError:
                continue

    # Get git info from working directory
    git_remote = None
    git_commit = None
    git_dirty = None
    if cwd:
        cwd_path = Path(cwd)
        if cwd_path.exists():
            git_remote = get_git_remote(cwd_path)
            git_commit = get_git_commit(cwd_path)
            git_dirty = get_git_dirty(cwd_path)

    # Calculate session duration
    started_at = None
    ended_at = None
    duration_seconds = None
    if first_msg and "timestamp" in first_msg:
        started_at = first_msg["timestamp"]
    if last_msg and "timestamp" in last_msg:
        ended_at = last_msg["timestamp"]
    if started_at and ended_at:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            duration_seconds = round((end - start).total_seconds(), 2)
        except (ValueError, TypeError):
            pass

    # Get system info
    try:
        tz_name = datetime.now().astimezone().tzname()
    except:
        tz_name = None

    return {
        # Identity
        "session_id": session_id,
        "is_agent_session": is_agent_session,
        "parent_session_id": parent_session_id,

        # Machine info
        "machine_id": config["machine_id"],
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "platform": platform.system().lower(),
        "platform_version": platform.release(),
        "timezone": tz_name,

        # Project/path info
        "project_name": project_name,
        "original_path": original_path,
        "cwd": cwd,

        # Git context
        "git_remote": git_remote,
        "git_branch": git_branch,
        "git_commit": git_commit,
        "git_dirty": git_dirty,

        # Claude info
        "claude_version": claude_version,
        "model": model_used,

        # Message stats
        "message_count": message_count,
        "user_message_count": user_message_count,
        "assistant_message_count": assistant_message_count,

        # Token usage
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_creation_tokens": total_cache_creation_tokens,

        # Tool usage
        "tool_calls_count": tool_calls_count,
        "tools_used": sorted(list(tools_used)),
        "files_modified": sorted(list(files_modified)),

        # Timestamps
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "synced_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),

        # Source reference
        "source_file": str(session_path),
    }


# === Sync Logic ===

def get_synced_sessions(repo_path: Path) -> set:
    """Get set of already-synced session IDs (flat metadata dir)."""
    metadata_dir = repo_path / "metadata"
    if not metadata_dir.exists():
        return set()
    return {p.stem for p in metadata_dir.glob("*.json")}


def sync_session(session_path: Path, config: dict, repo_path: Path) -> bool:
    """Sync a single session to the repo. Returns True if synced."""

    session_id = session_path.stem

    # Extract metadata
    metadata = extract_session_metadata(session_path, config)

    # Determine date folder from session start
    if metadata["started_at"]:
        date_str = metadata["started_at"][:10]  # YYYY-MM-DD
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Create directories (flat structure)
    session_dir = repo_path / "sessions" / date_str
    metadata_dir = repo_path / "metadata"
    session_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    # Copy session file
    dest_session = session_dir / f"{session_id}.jsonl"

    # Optionally filter out thinking blocks
    if config.get("include_thinking", False):
        # Just copy the file as-is
        import shutil
        shutil.copy2(session_path, dest_session)
    else:
        # Filter out thinking content to reduce size
        with open(session_path) as src, open(dest_session, "w") as dst:
            for line in src:
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                    # Remove thinking blocks from assistant messages
                    if msg.get("type") == "assistant" and "message" in msg:
                        content = msg["message"].get("content", [])
                        if isinstance(content, list):
                            msg["message"]["content"] = [
                                c for c in content if c.get("type") != "thinking"
                            ]
                    dst.write(json.dumps(msg) + "\n")
                except json.JSONDecodeError:
                    dst.write(line)

    # Write metadata (flat)
    metadata_path = metadata_dir / f"{session_id}.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    return True


def sync_all(config: dict, push: bool = False) -> dict:
    """Sync all new sessions. Returns stats."""

    repo_path = Path(config["sync_repo_path"])
    claude_path = Path(config["claude_projects_path"])
    machine_id = config["machine_id"]

    if not repo_path.exists():
        print(f"Sync repo not found at {repo_path}. Run with --init first.")
        return {"error": "Repo not initialized"}

    if not claude_path.exists():
        print(f"Claude projects not found at {claude_path}")
        return {"error": "Claude projects not found"}

    # Get already synced sessions (flat)
    synced = get_synced_sessions(repo_path)
    print(f"Found {len(synced)} already-synced sessions")

    # Find all session files
    new_sessions = []
    for project_dir in claude_path.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if session_file.stem not in synced:
                new_sessions.append(session_file)

    print(f"Found {len(new_sessions)} new sessions to sync")

    # Sync each session
    synced_count = 0
    for session_path in new_sessions:
        try:
            if sync_session(session_path, config, repo_path):
                synced_count += 1
                print(f"  ✓ {session_path.stem[:12]}... ({extract_project_name(session_path.parent.name)})")
        except Exception as e:
            print(f"  ✗ {session_path.stem[:12]}... Error: {e}")

    # Git commit
    if synced_count > 0:
        run_git(repo_path, "add", ".")
        commit_msg = f"Sync {synced_count} sessions from {machine_id}"
        run_git(repo_path, "commit", "--no-verify", "-m", commit_msg)
        print(f"\nCommitted: {commit_msg}")

        if push:
            pull = run_git(repo_path, "pull", "--rebase", "--no-verify")
            if pull.returncode != 0:
                print(f"Pull failed: {pull.stderr}")
            result = run_git(repo_path, "push", "--no-verify")
            if result.returncode == 0:
                print("Pushed to remote")
            else:
                print(f"Push failed: {result.stderr}")

    return {
        "machine_id": machine_id,
        "previously_synced": len(synced),
        "newly_synced": synced_count,
        "total": len(synced) + synced_count,
    }


def show_status(config: dict):
    """Show sync status."""
    repo_path = Path(config["sync_repo_path"])
    claude_path = Path(config["claude_projects_path"])
    machine_id = config["machine_id"]

    print(f"Machine ID: {machine_id}")
    print(f"Sync repo: {repo_path}")
    print(f"Claude projects: {claude_path}")
    print()

    if not repo_path.exists():
        print("Status: Not initialized (run with --init)")
        return

    # Count synced sessions (flat)
    synced = get_synced_sessions(repo_path)

    # Count total local sessions
    total_local = 0
    for project_dir in claude_path.iterdir():
        if project_dir.is_dir() and not project_dir.name.startswith("."):
            total_local += len(list(project_dir.glob("*.jsonl")))

    pending = total_local - len(synced)

    print(f"Local sessions: {total_local}")
    print(f"Synced: {len(synced)}")
    print(f"Pending: {pending}")

    # Check git status
    result = run_git(repo_path, "status", "--porcelain")
    if result.stdout.strip():
        print("\nUncommitted changes in sync repo")

    # Check remote
    result = run_git(repo_path, "remote", "-v")
    if result.stdout.strip():
        print(f"\nRemote: {result.stdout.strip().split()[1]}")
    else:
        print("\nNo remote configured")


# === CLI ===

def main():
    parser = argparse.ArgumentParser(description="Sync Claude Code conversations to git")
    parser.add_argument("--init", action="store_true", help="Initialize config and sync repo")
    parser.add_argument("--status", action="store_true", help="Show sync status")
    parser.add_argument("--push", action="store_true", help="Push to remote after sync")
    parser.add_argument("--remote", type=str, help="Git remote URL (for --init)")
    parser.add_argument("--machine-id", type=str, help="Override machine ID")

    args = parser.parse_args()

    config = load_config()

    if args.machine_id:
        config["machine_id"] = args.machine_id

    if args.init:
        save_config(config)
        init_sync_repo(Path(config["sync_repo_path"]), args.remote)
        return

    if args.status:
        show_status(config)
        return

    # Default: sync
    stats = sync_all(config, push=args.push)
    print(f"\nSync complete: {stats}")


if __name__ == "__main__":
    main()
