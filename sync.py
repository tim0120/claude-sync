#!/usr/bin/env python3
"""
claude-sync: Sync Claude Code conversations to a git repo with machine metadata.

Usage:
    python sync.py                    # Sync all new conversations
    python sync.py --init             # Initialize config
    python sync.py --status           # Show sync status
"""

import json
import os
import subprocess
import socket
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
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
    result = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def get_git_branch(path: Path) -> Optional[str]:
    """Get current git branch for a path."""
    result = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def init_sync_repo(repo_path: Path, remote_url: Optional[str] = None):
    """Initialize the sync repo."""
    repo_path.mkdir(parents=True, exist_ok=True)

    if not (repo_path / ".git").exists():
        run_git(repo_path, "init")

        # Create initial structure
        (repo_path / "sessions").mkdir(exist_ok=True)
        (repo_path / "metadata").mkdir(exist_ok=True)

        # Create README
        readme = repo_path / "README.md"
        readme.write_text("""# Claude Sync Repository

This repository contains synced Claude Code conversations from multiple machines.

## Structure

```
sessions/
  <machine-id>/
    <date>/
      <session-id>.jsonl      # Raw conversation
metadata/
  <machine-id>/
    <session-id>.json         # Enriched metadata
```

## Querying

Use the claude-history MCP to search across all synced conversations.
""")

        run_git(repo_path, "add", ".")
        run_git(repo_path, "commit", "-m", "Initial commit")

        if remote_url:
            run_git(repo_path, "remote", "add", "origin", remote_url)

        print(f"Initialized sync repo at {repo_path}")
    else:
        print(f"Sync repo already exists at {repo_path}")


# === Metadata Extraction ===

def extract_session_metadata(session_path: Path, config: dict) -> dict:
    """Extract metadata from a session file."""

    # Parse the project path from the directory name
    # Format: -Users-timkostolansky-Developer-workshop-labs-timbox
    project_dir = session_path.parent.name
    # Remove leading dash, then replace remaining dashes with slashes
    original_path = "/" + project_dir.lstrip("-").replace("-", "/")

    # Read first and last messages to get timestamps
    first_msg = None
    last_msg = None
    message_count = 0
    git_branch = None
    git_remote = None
    model_used = None
    cwd = None

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

                # Extract git info from messages
                if "gitBranch" in msg and not git_branch:
                    git_branch = msg["gitBranch"]
                if "cwd" in msg and not cwd:
                    cwd = msg["cwd"]

                # Extract model from assistant messages
                if msg.get("type") == "assistant" and "message" in msg:
                    if "model" in msg["message"]:
                        model_used = msg["message"]["model"]

            except json.JSONDecodeError:
                continue

    # Try to get git remote from the working directory
    if cwd:
        git_remote = get_git_remote(Path(cwd))

    # Calculate session duration
    duration_seconds = None
    if first_msg and last_msg:
        try:
            start = datetime.fromisoformat(first_msg["timestamp"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(last_msg["timestamp"].replace("Z", "+00:00"))
            duration_seconds = (end - start).total_seconds()
        except (KeyError, ValueError):
            pass

    return {
        "session_id": session_path.stem,
        "machine_id": config["machine_id"],
        "original_path": original_path,
        "cwd": cwd,
        "git_remote": git_remote,
        "git_branch": git_branch,
        "model": model_used,
        "message_count": message_count,
        "started_at": first_msg.get("timestamp") if first_msg else None,
        "ended_at": last_msg.get("timestamp") if last_msg else None,
        "duration_seconds": duration_seconds,
        "synced_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_file": str(session_path),
    }


# === Sync Logic ===

def get_synced_sessions(repo_path: Path, machine_id: str) -> set:
    """Get set of already-synced session IDs for this machine."""
    metadata_dir = repo_path / "metadata" / machine_id
    if not metadata_dir.exists():
        return set()
    return {p.stem for p in metadata_dir.glob("*.json")}


def sync_session(session_path: Path, config: dict, repo_path: Path) -> bool:
    """Sync a single session to the repo. Returns True if synced."""

    session_id = session_path.stem
    machine_id = config["machine_id"]

    # Extract metadata
    metadata = extract_session_metadata(session_path, config)

    # Determine date folder from session start
    if metadata["started_at"]:
        date_str = metadata["started_at"][:10]  # YYYY-MM-DD
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Create directories
    session_dir = repo_path / "sessions" / machine_id / date_str
    metadata_dir = repo_path / "metadata" / machine_id
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

    # Write metadata
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

    # Get already synced sessions
    synced = get_synced_sessions(repo_path, machine_id)
    print(f"Found {len(synced)} already-synced sessions for {machine_id}")

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
                print(f"  ✓ {session_path.stem[:8]}... ({session_path.parent.name})")
        except Exception as e:
            print(f"  ✗ {session_path.stem[:8]}... Error: {e}")

    # Git commit
    if synced_count > 0:
        run_git(repo_path, "add", ".")
        commit_msg = f"Sync {synced_count} sessions from {machine_id}"
        run_git(repo_path, "commit", "-m", commit_msg)
        print(f"\nCommitted: {commit_msg}")

        if push:
            result = run_git(repo_path, "push")
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

    # Count synced sessions
    synced = get_synced_sessions(repo_path, machine_id)

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
