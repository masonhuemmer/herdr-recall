#!/usr/bin/env python3
"""Hourly, private Herdr snapshots and explicit recovery. Python 3.10+, POSIX."""

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid


DEFAULT_STORE = Path.home() / '.local/state/herdr-recall'
LABEL = 'io.local.herdr-recall'
AGENT_FLAGS = {
    'codex': '--dangerously-bypass-approvals-and-sandbox',
    'claude': '--dangerously-skip-permissions',
}


class RecoveryError(Exception):
    pass


def cli_env():
    # An hourly job must enumerate all local sessions, even when installed in a pane.
    return {k: v for k, v in os.environ.items()
            if not k.startswith('HERDR_') or k == 'HERDR_CONFIG_PATH'}


def herdr(binary, *args, session=None):
    command = [binary]
    if session is not None:
        command += ['--session', session]
    result = subprocess.run(command + list(args), env=cli_env(),
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RecoveryError(f'Herdr {args[0]} failed: {result.stderr.strip()}')
    # Mutating CLI commands such as `pane run` can succeed without JSON output.
    return json.loads(result.stdout) if result.stdout.strip() else {}


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path, data):
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + '\n').encode())


@contextlib.contextmanager
def locked(store):
    private_dir(store)
    with (store / '.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError('Another save or restore is running.') from exc
        yield


def valid_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def leaf_ids(layout):
    if 'Pane' in layout:
        return [str(layout['Pane'])]
    split = layout['Split']
    return leaf_ids(split['first']) + leaf_ids(split['second'])


def tab_pairs(state, live):
    """Match by saved order and BSP leaf order; public IDs can change on restart."""
    if len(state['workspaces']) != len(live['workspaces']):
        raise RecoveryError('Workspace topology changed during capture/restore.')
    layouts = {v['tab_id']: v for v in live['layouts']}
    pairs = []
    for wi, (saved_ws, live_ws) in enumerate(zip(state['workspaces'], live['workspaces'])):
        if saved_ws.get('id') and saved_ws['id'] != live_ws['workspace_id']:
            raise RecoveryError('Workspace identity changed during capture/restore.')
        if saved_ws.get('custom_name') and saved_ws['custom_name'] != live_ws['label']:
            raise RecoveryError('Workspace order changed during capture/restore.')
        live_tabs = [t for t in live['tabs'] if t['workspace_id'] == live_ws['workspace_id']]
        if len(saved_ws['tabs']) != len(live_tabs):
            raise RecoveryError('Tab topology changed during capture/restore.')
        for ti, (saved_tab, live_tab) in enumerate(zip(saved_ws['tabs'], live_tabs)):
            if saved_tab.get('custom_name') and saved_tab['custom_name'] != live_tab['label']:
                raise RecoveryError('Tab order changed during capture/restore.')
            layout = layouts[live_tab['tab_id']]
            old_ids = leaf_ids(saved_tab['layout'])
            new_ids = [p['pane_id'] for p in layout['panes']]
            if len(old_ids) != len(new_ids):
                raise RecoveryError('Pane topology changed during capture/restore.')
            pairs.extend((wi, ti, old, new) for old, new in zip(old_ids, new_ids))
    return pairs


class SessionFiles:
    def __init__(self):
        self.roots = {
            'codex': Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser().resolve(),
            'claude': Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')).expanduser().resolve(),
        }
        self.codex = {}
        # Read the current SQLite WAL through SQLite, never by copying a live DB.
        for database in sorted(self.roots['codex'].glob('state_*.sqlite'), reverse=True):
            try:
                with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                    columns = {r[1] for r in db.execute('pragma table_info(threads)')}
                    name = 'name' if 'name' in columns else 'NULL'
                    for sid, cwd, title, path in db.execute(
                            f'select id,cwd,{name},rollout_path from threads'):
                        self.codex.setdefault(sid, {'cwd': cwd, 'name': title, 'path': path})
            except sqlite3.Error:
                continue

    def find(self, agent, sid):
        root = self.roots[agent]
        if agent == 'codex':
            record = self.codex.get(sid)
            if record and Path(record['path']).is_file():
                return Path(record['path'])
            matches = list((root / 'sessions').glob(f'**/*{sid}.jsonl'))
            matches += list((root / 'archived_sessions').glob(f'*{sid}.jsonl'))
        else:
            matches = list((root / 'projects').glob(f'*/{sid}.jsonl'))
        if len(matches) == 1:
            return matches[0]
        raise RecoveryError(f'Cannot uniquely locate {agent} transcript {sid}.')

    def identify(self, pane, processes):
        agent = pane.get('agent')
        native = pane.get('agent_session') or {}
        if native.get('agent') == agent and native.get('kind') == 'id':
            sid = valid_id(native.get('value'))
            if sid:
                return sid, 'herdr-integration'
        for process in processes:
            if agent == 'claude':
                record = self.roots['claude'] / 'sessions' / f"{process['pid']}.json"
                if record.exists():
                    value = json.loads(record.read_text())
                    sid = valid_id(value.get('sessionId'))
                    if sid and value.get('pid') == process['pid']:
                        return sid, 'claude-pid-record'
        if agent == 'codex':
            # Exact name + cwd only. Never choose the most recent session in a cwd.
            title = re.sub(r'^[^\w]*', '', pane.get('terminal_title_stripped') or '')
            title = title.rsplit(' | ', 1)[0]
            cwd = pane.get('foreground_cwd') or pane.get('cwd')
            matches = [sid for sid, r in self.codex.items()
                       if title and r['name'] == title and r['cwd'] == cwd]
            if len(matches) == 1:
                return matches[0], 'unique-codex-name-and-cwd'
            if len(matches) > 1:
                raise RecoveryError(f"Ambiguous Codex session in {pane['pane_id']}.")
        for process in processes:
            argv = process.get('argv') or []
            for i, argument in enumerate(argv[:-1]):
                if argument in ('resume', '--resume', '-r', '--session-id'):
                    sid = valid_id(argv[i + 1])
                    if sid:
                        return sid, 'process-resume-argument'
        raise RecoveryError(f"No exact {agent} session ID for {pane['pane_id']}.")


def transcript_artifact(files, agent, sid, store):
    path = files.find(agent, sid)
    root = files.roots[agent]
    relative = path.resolve().relative_to(root.resolve())
    # A process may be writing its last JSONL record. Keep complete records only.
    content = path.read_bytes()
    content = content[:content.rfind(b'\n') + 1]
    if not content:
        raise RecoveryError(f'Empty transcript: {agent} {sid}')
    digest = hashlib.sha256(content).hexdigest()
    blob = store / 'blobs' / (digest + '.jsonl.gz')
    if not blob.exists():
        atomic_write(blob, gzip.compress(content, compresslevel=6, mtime=0))
    return {
        'agent': agent, 'session_id': sid, 'path': str(relative),
        'sha256': digest,
    }


def capture_session(binary, info, files, artifacts, store):
    path = Path(info['session_dir']) / 'session.json'
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if state.get('version') != 3:
        raise RecoveryError(f"Unsupported Herdr state version in {info['name']}.")
    agents = []
    if info['running']:
        live = herdr(binary, 'api', 'snapshot', session=info['name'])['result']['snapshot']
        panes = {p['pane_id']: p for p in live['panes']}
        for wi, ti, internal, public in tab_pairs(state, live):
            pane = panes[public]
            saved = state['workspaces'][wi]['tabs'][ti]['panes'][internal]
            saved['cwd'] = pane.get('foreground_cwd') or pane.get('cwd') or saved['cwd']
            if pane.get('agent') not in AGENT_FLAGS:
                # Do not revive an old agent that has since exited this pane.
                saved.pop('agent_session', None)
                continue
            processes = herdr(binary, 'pane', 'process-info', '--pane', public,
                              session=info['name'])['result']['process_info']['foreground_processes']
            sid, source = files.identify(pane, processes)
            agent = pane['agent']
            agents.append({'workspace': wi, 'tab': ti, 'pane': internal,
                           'agent': agent, 'session_id': sid, 'identified_by': source})
            saved['agent_session'] = {'source': f'herdr:{agent}', 'agent': agent,
                                      'kind': 'id', 'value': sid}
        # Re-read before publishing: do not silently pair data across a layout edit.
        after = json.loads(path.read_text())
        topology = lambda s: [(w.get('id'), [(t.get('custom_name'), t['layout'])
                                            for t in w['tabs']]) for w in s['workspaces']]
        if topology(after) != topology(state):
            raise RecoveryError('Layout changed while saving; retry the snapshot.')
    else:
        for wi, ws in enumerate(state['workspaces']):
            for ti, tab in enumerate(ws['tabs']):
                for internal in leaf_ids(tab['layout']):
                    ref = tab['panes'][internal].get('agent_session') or {}
                    if ref.get('agent') in AGENT_FLAGS:
                        sid = valid_id(ref.get('value'))
                        if not sid:
                            raise RecoveryError('Invalid saved agent session reference.')
                        agents.append({'workspace': wi, 'tab': ti, 'pane': internal,
                                       'agent': ref['agent'], 'session_id': sid,
                                       'identified_by': 'stopped-herdr-state'})
    for a in agents:
        key = a['agent'] + ':' + a['session_id']
        if key not in artifacts:
            artifacts[key] = transcript_artifact(files, a['agent'], a['session_id'], store)
    return {'name': info['name'], 'was_running': info['running'],
            'state': state, 'agents': agents}


def save(args):
    with locked(args.store):
        infos = herdr(args.herdr, 'session', 'list', '--json')['sessions']
        if not any(i['running'] for i in infos):
            print('Herdr is stopped; keeping the last recovery snapshot.')
            return
        files, artifacts, sessions = SessionFiles(), {}, []
        for info in infos:
            session = capture_session(args.herdr, info, files, artifacts, args.store)
            if session:
                sessions.append(session)
        if not sessions or not any(s['state']['workspaces'] for s in sessions):
            raise RecoveryError('Refusing to replace the last snapshot with an empty layout.')
        now = dt.datetime.now(dt.timezone.utc)
        snapshot = {'format_version': 1, 'created_at': now.isoformat(),
                    'sessions': sessions, 'artifacts': list(artifacts.values())}
        name = now.strftime('%Y%m%dT%H%M%S.%fZ.json')
        path = args.store / 'snapshots' / name
        write_json(path, snapshot)
        link = args.store / '.latest.json'
        link.unlink(missing_ok=True)
        link.symlink_to(Path('snapshots') / name)
        os.replace(link, args.store / 'latest.json')
        snapshots = sorted(path.parent.glob('*.json'))
        for old in snapshots[:-args.keep]:
            old.unlink()
        retained = set()
        for remaining in sorted(path.parent.glob('*.json')):
            retained.update(a['sha256'] for a in json.loads(remaining.read_text())['artifacts'])
        for blob in (args.store / 'blobs').glob('*.jsonl.gz'):
            if blob.name.removesuffix('.jsonl.gz') not in retained:
                blob.unlink()
        count = sum(len(s['agents']) for s in sessions)
        print(f'Saved {len(sessions)} Herdr session(s), {count} agents: {path}')


def resume_argv(agent, sid):
    if agent == 'codex':
        return ['codex', 'resume', sid, AGENT_FLAGS[agent]]
    return ['claude', '--resume', sid, AGENT_FLAGS[agent]]


def artifact_data(artifact, store):
    digest = artifact['sha256']
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise RecoveryError('Invalid transcript checksum.')
    data = gzip.decompress((store / 'blobs' / (digest + '.jsonl.gz')).read_bytes())
    if hashlib.sha256(data).hexdigest() != digest:
        raise RecoveryError('Transcript checksum failed.')
    return data


def validate_snapshot(snapshot, store):
    if snapshot.get('format_version') != 1 or not snapshot.get('sessions'):
        raise RecoveryError('Unsupported or empty recovery snapshot.')
    for session in snapshot['sessions']:
        if session['state'].get('version') != 3:
            raise RecoveryError('Unsupported Herdr layout format.')
        for agent in session['agents']:
            if agent['agent'] not in AGENT_FLAGS or not valid_id(agent['session_id']):
                raise RecoveryError('Invalid agent reference in snapshot.')
            pane = session['state']['workspaces'][agent['workspace']]['tabs'][agent['tab']]['panes']
            if agent['pane'] not in pane:
                raise RecoveryError('Agent points to a missing pane.')
    for artifact in snapshot['artifacts']:
        path = Path(artifact['path'])
        allowed = {'codex': ('sessions', 'archived_sessions'), 'claude': ('projects',)}
        if (artifact['agent'] not in allowed or path.is_absolute() or '..' in path.parts
                or not path.parts or path.parts[0] not in allowed[artifact['agent']]
                or path.suffix != '.jsonl' or not valid_id(artifact['session_id'])):
            raise RecoveryError('Invalid transcript path in snapshot.')
        artifact_data(artifact, store)


def restore_transcripts(snapshot, files, store):
    for artifact in snapshot['artifacts']:
        path = files.roots[artifact['agent']] / artifact['path']
        if not path.resolve().is_relative_to(files.roots[artifact['agent']].resolve()):
            raise RecoveryError('Transcript path escapes its agent home.')
        # Existing conversations may contain work newer than the hourly snapshot.
        if not path.exists():
            atomic_write(path, artifact_data(artifact, store))


def shell_state(state):
    state = copy.deepcopy(state)
    for ws in state['workspaces']:
        for tab in ws['tabs']:
            for pane in tab['panes'].values():
                pane.pop('agent_session', None)
                pane.pop('launch_argv', None)
    return state


def restore(args):
    path = args.snapshot or args.store / 'latest.json'
    snapshot = json.loads(path.read_text())
    validate_snapshot(snapshot, args.store)
    sessions = [s for s in snapshot['sessions'] if not args.session or s['name'] == args.session]
    if not sessions:
        raise RecoveryError('The requested session is absent from this snapshot.')
    if args.as_session and len(sessions) != 1:
        raise RecoveryError('--as-session requires selecting one source session with --session.')
    infos = herdr(args.herdr, 'session', 'list', '--json')['sessions']
    known = {i['name']: i for i in infos}
    default_dir = Path(next(i['session_dir'] for i in infos if i['default']))
    targets = []
    for session in sessions:
        name = args.as_session or session['name']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
            raise RecoveryError('Invalid target session name.')
        if known.get(name, {}).get('running'):
            raise RecoveryError(f'{name} is running. Stop it yourself or use --as-session NAME.')
        directory = default_dir if name == 'default' else default_dir / 'sessions' / name
        targets.append((session, name, directory))
    for session, name, _ in targets:
        print(f"Restore {name}: {len(session['state']['workspaces'])} workspaces, "
              f"{len(session['agents'])} agents")
        for agent in session['agents']:
            print('  ' + shlex.join(resume_argv(agent['agent'], agent['session_id'])))
    if args.dry_run:
        return
    # Preflight every directory before replacing any state or starting any server.
    for session, _, _ in targets:
        for ws in session['state']['workspaces']:
            for tab in ws['tabs']:
                for pane in tab['panes'].values():
                    if not Path(pane['cwd']).is_dir():
                        raise RecoveryError(f"Missing pane directory: {pane['cwd']}")
    with locked(args.store):
        restore_transcripts(snapshot, SessionFiles(), args.store)
        for session, name, directory in targets:
            private_dir(directory)
            current = directory / 'session.json'
            if current.exists():
                stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
                atomic_write(directory / f'session.pre-recovery-{stamp}.json', current.read_bytes())
            # Native automatic restore uses canonical commands without our flags.
            # Start shells from the layout, then issue each explicit resume once.
            write_json(current, shell_state(session['state']))
            with (directory / 'herdr-recall-start.log').open('ab') as log:
                process = subprocess.Popen([args.herdr, '--session', name, 'server'],
                                           env=cli_env(), stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
            # Reap the child if used as a library; CLI exit leaves the server running.
            threading.Thread(target=process.wait, daemon=True).start()
            deadline = time.monotonic() + 20
            live = None
            while time.monotonic() < deadline:
                try:
                    live = herdr(args.herdr, 'api', 'snapshot', session=name)['result']['snapshot']
                    if live['panes']:
                        break
                except (RecoveryError, json.JSONDecodeError):
                    pass
                if process.poll() is not None:
                    raise RecoveryError(f'Herdr {name} exited; inspect {directory}/herdr-recall-start.log')
                time.sleep(0.1)
            if not live or not live['panes']:
                raise RecoveryError(f'Timed out starting {name}.')
            mapping = {(wi, ti, old): new for wi, ti, old, new
                       in tab_pairs(session['state'], live)}
            for agent in session['agents']:
                pane = mapping[agent['workspace'], agent['tab'], agent['pane']]
                # Wait for shell startup, and never type into a foreground program.
                deadline = time.monotonic() + 15
                while True:
                    info = herdr(args.herdr, 'pane', 'process-info', '--pane', pane,
                                 session=name)['result']['process_info']
                    foreground = info.get('foreground_processes', [])
                    if foreground and all(p['pid'] == info.get('shell_pid') for p in foreground):
                        break
                    if time.monotonic() > deadline:
                        raise RecoveryError(f'{name}/{pane} did not reach an available shell.')
                    time.sleep(0.1)
                command = shlex.join(resume_argv(agent['agent'], agent['session_id']))
                herdr(args.herdr, 'pane', 'run', pane, command, session=name)
                deadline = time.monotonic() + 20
                while True:
                    info = herdr(args.herdr, 'pane', 'process-info', '--pane', pane,
                                 session=name)['result']['process_info']
                    foreground = info.get('foreground_processes', [])
                    if any(AGENT_FLAGS[agent['agent']] in p.get('argv', [])
                           and agent['session_id'] in p.get('argv', []) for p in foreground):
                        break
                    if time.monotonic() > deadline:
                        raise RecoveryError(f'Agent did not start in {name}/{pane}; inspect that pane.')
                    time.sleep(0.1)
            print(f'Restored {name}. Attach with: herdr --session {shlex.quote(name)}')


def install(args):
    if sys.platform != 'darwin':
        raise RecoveryError('Automatic scheduling currently uses macOS launchd.')
    private_dir(args.store)
    script = Path(__file__).resolve()
    launcher = Path.home() / '.local/bin/herdr-recall'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.exists() or launcher.is_symlink():
        if not launcher.is_symlink() or launcher.resolve() != script:
            raise RecoveryError(f'Refusing to replace {launcher}.')
    else:
        launcher.symlink_to(script)
    script.chmod(0o755)
    env = {'PATH': os.pathsep.join([str(Path.home() / '.local/bin'),
                                   '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])}
    for key in ('HERDR_CONFIG_PATH', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME',
                'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        if key in os.environ:
            env[key] = os.environ[key]
    plist = {
        'Label': LABEL,
        'ProgramArguments': [sys.executable, str(script), '--store', str(args.store),
                             '--herdr', args.herdr, 'save', '--keep', str(args.keep)],
        'EnvironmentVariables': env, 'StartInterval': 3600, 'RunAtLoad': True,
        'Umask': 0o077,
        'StandardOutPath': str(args.store / 'hourly.log'),
        'StandardErrorPath': str(args.store / 'hourly-error.log'),
    }
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    # Do not chmod the shared LaunchAgents directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, 'wb') as stream:
        plistlib.dump(plist, stream)
    os.replace(temporary, path)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL],
                   capture_output=True, check=False)
    subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    print(f'Hourly snapshots enabled. Retention: {args.keep} snapshots. Store: {args.store}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=DEFAULT_STORE)
    parser.add_argument('--herdr', default=shutil.which('herdr') or 'herdr')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('save', 'install'):
        child = sub.add_parser(name)
        child.add_argument('--keep', type=int, default=168, help='Retained snapshots (default: 168)')
    child = sub.add_parser('restore')
    child.add_argument('--snapshot', type=Path)
    child.add_argument('--session', help='Select a source session; default: all')
    child.add_argument('--as-session', help='Restore selected source into a different session')
    child.add_argument('--dry-run', action='store_true')
    sub.add_parser('list')
    sub.add_parser('uninstall')
    args = parser.parse_args()
    args.store = args.store.expanduser().resolve()
    if hasattr(args, 'keep') and args.keep < 1:
        parser.error('--keep must be at least 1')
    try:
        if args.command == 'save':
            save(args)
        elif args.command == 'restore':
            restore(args)
        elif args.command == 'install':
            install(args)
        elif args.command == 'uninstall':
            domain = f'gui/{os.getuid()}/{LABEL}'
            subprocess.run(['launchctl', 'bootout', domain], capture_output=True, check=False)
            (Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')).unlink(missing_ok=True)
            print('Hourly job removed; snapshots and script retained.')
        else:
            for path in sorted((args.store / 'snapshots').glob('*.json'), reverse=True):
                print(path)
    except (RecoveryError, OSError, ValueError, KeyError, IndexError,
            subprocess.SubprocessError) as exc:
        print(f'herdr-recall: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
