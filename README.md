# herdr-recall

Save all local Herdr sessions hourly and recover workspaces, tabs, split panes,
directories, focus, and exact Claude Code / Codex conversations after a reboot,
crash, or Herdr upgrade. The script uses Python's standard library.

## Install on macOS

Requires Python 3.10+, Herdr on `PATH`, and the `claude` / `codex` CLIs used by
your saved panes. Tested with Herdr 0.9.1 and its version 3 session format.

```sh
git clone https://github.com/masonhuemmer/herdr-recall.git
cd herdr-recall
python3 herdr_recall.py save
python3 herdr_recall.py install
```

Installation creates `~/.local/bin/herdr-recall` pointing to this checkout and a
per-user launchd job, `io.local.herdr-recall`. Keep the checkout in place.
The job runs at login and every 3,600 seconds while you are logged in. macOS
does not run the job while the machine is powered off; sleeping can delay a run.
The interpreter and Herdr executable paths are recorded at installation. Rerun
`install` if either moves after an upgrade.

```sh
herdr-recall save                 # Save immediately before an upgrade/reboot
herdr-recall list                 # List retained snapshots, newest first
herdr-recall uninstall            # Remove the job; keep backups and the script
```

## Recover after reboot or upgrade

Run recovery **before starting Herdr**:

```sh
herdr-recall restore --dry-run
herdr-recall restore
herdr
```

`restore` reads the latest complete snapshot, starts each saved session's
headless Herdr server, recreates its layout, and resumes each supported agent in
its original pane using:

```sh
codex resume SESSION_ID --dangerously-bypass-approvals-and-sandbox
claude --resume SESSION_ID --dangerously-skip-permissions
```

Recovery runs agents without sandboxing or approval prompts. Restore only
backups you created and trust. No prompt or new task
is submitted to an agent. Herdr's automatic agent restore is suppressed for this
recovery by removing native launch references from the layout written at startup;
the tool then launches each explicit resume command and confirms that the process
started. It does not change Herdr's configuration or your normal launch commands.

Recovery refuses to replace a running target session. If Herdr has already
started, use a different session name, or stop the affected session yourself
before recovering it. Stopping a session terminates its pane processes.

```sh
# Recover one source into a separate session; keep the current session running.
herdr-recall restore --session default --as-session recovered
herdr --session recovered

# Select a retained snapshot instead of the latest one.
herdr-recall restore --snapshot ~/.local/state/herdr-recall/snapshots/TIMESTAMP.json

# Restore just one named session from the snapshot.
herdr-recall restore --session work
herdr --session work
```

An existing stopped target's `session.json` is copied to a timestamped
`session.pre-recovery-*.json` before replacement. Missing working directories,
corrupt transcripts, and unknown IDs fail recovery rather than launching a
different conversation. If recovery fails after starting a server, inspect its
panes and `herdr-recall-start.log` in that session's directory. The original
recovery snapshot is retained; stop that target yourself before retrying.

## Backup files and retention

The default store is `~/.local/state/herdr-recall/`:

```text
latest.json -> snapshots/TIMESTAMP.json
snapshots/    # JSON manifests: layout, session IDs, transcript checksums
blobs/        # Compressed, deduplicated conversation files
hourly.log
hourly-error.log
```

The default is 168 snapshots, approximately seven days of hourly captures.
Unchanged transcript versions share a compressed blob. Blobs are removed only
after no retained snapshot references them. Active conversations can grow large;
disk usage depends on their size and how often they change.

**Copy the whole store, including `blobs/`, to move or independently back up a
recovery snapshot.** The JSON manifest alone does not contain conversation text.
To restore a copied store, specify it before the subcommand:

```sh
herdr-recall --store /path/to/copied-store restore
herdr-recall --store /path/to/backups install --keep 48
```

The store uses private directories and files. Conversation files can contain
private material and command output. Backups remain local and are excluded from
Git; the script never uploads them or copies authentication files. This protects
against session loss on the same machine, not loss of the disk itself.

The script honors `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `HERDR_CONFIG_PATH`, and XDG
config/state paths. Installation records these overrides for the scheduled job.
Shells launched by Herdr must be able to find the agent executables on `PATH`.

## Capture and recovery behavior

- Enumerates the default session and all named local sessions. Running sessions
  use Herdr's live API plus persisted layout; stopped sessions use their persisted
  layout and agent references. Saved public pane IDs may change during recovery;
  agents are mapped through workspace, tab, and layout positions.
- Prefers integration-reported session IDs. Claude can also use its PID session
  record. Codex can use a unique conversation name plus exact working directory
  from its local SQLite database, or an explicit resume ID in process arguments.
  The process-argument fallback reflects the launch ID; native reporting is more
  reliable if you switch conversations inside an existing process. The tool
  never selects the newest conversation merely because it shares a directory.
- Archives the primary JSONL conversation file for each identified agent, using
  complete records and SHA-256 verification. Newer existing local transcripts
  are preserved on recovery; missing transcripts are recreated. Auth, plugins,
  agent configuration, attachments, and subagent files are not archived.
- A capture failure leaves the previous latest snapshot intact. If every Herdr
  session is stopped, the hourly job keeps the previous recovery snapshot.
- Saves are atomic and save/restore operations share a lock. Inspect
  `hourly-error.log` if a new agent cannot be identified or its transcript is
  missing. Run `save` once before relying on the scheduled job.
- Ordinary terminal panes return as shells in their saved directories. Shell
  memory, environment changes, terminal scrollback, arbitrary foreground
  programs, and unfinished tool calls cannot be restored by this script.
- Backup and restore target local POSIX installations. Automatic scheduling is
  macOS-only. Remote SSH Herdr servers and saved remote machine profiles are not
  enumerated.

Herdr's own restoration behavior is documented in
[Session state and restore](https://herdr.dev/docs/session-state/). The tool uses
the installed CLI's API schema and native state format rather than UI automation.
The Codex resume syntax is also documented in the
[Codex CLI reference](https://developers.openai.com/codex/cli/reference/).

## Tests

```sh
python3 -m unittest discover -s tests -v
HERDR_INTEGRATION_TEST=1 python3 -m unittest discover -s tests -v
```

The integration test starts a real Herdr server under a temporary XDG directory,
restores multiple workspaces and tabs with split/zoom state, and uses harmless
agent stubs to verify exact arguments and working directories. It stops only its
own temporary server and does not launch a real AI session.
