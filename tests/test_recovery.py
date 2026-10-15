import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herdr_recall as recovery


CODEX_ID = '11111111-1111-4111-8111-111111111111'
CLAUDE_ID = '22222222-2222-4222-8222-222222222222'


def fixture(cwd):
    def tab(name, ids):
        layout = {'Pane': ids[0]}
        if len(ids) == 2:
            layout = {'Split': {'direction': 'Horizontal', 'ratio': 0.35,
                                'first': layout, 'second': {'Pane': ids[1]}}}
        return {'custom_name': name, 'layout': layout,
                'panes': {str(i): {'cwd': str(cwd)} for i in ids},
                'zoomed': False, 'focused': ids[0], 'root_pane': ids[0]}
    state = {'version': 3, 'active': 1, 'selected': 1, 'workspaces': [
        {'id': 'w1', 'custom_name': 'Development', 'identity_cwd': str(cwd),
         'active_tab': 1, 'tabs': [tab('Agents', [1, 2]), tab('Shell', [3])]},
        {'id': 'w2', 'custom_name': 'Operations', 'identity_cwd': str(cwd),
         'active_tab': 0, 'tabs': [tab('Console', [4])]},
    ]}
    return {'format_version': 1, 'created_at': '2026-10-15T00:00:00Z',
            'sessions': [{'name': 'default', 'was_running': True, 'state': state,
                          'agents': [
                              {'workspace': 0, 'tab': 0, 'pane': '1',
                               'agent': 'codex', 'session_id': CODEX_ID},
                              {'workspace': 0, 'tab': 0, 'pane': '2',
                               'agent': 'claude', 'session_id': CLAUDE_ID}]}],
            'artifacts': []}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = fixture(self.root)
        self.store = self.root / 'store'
        self.store.mkdir()
        self.files = recovery.SessionFiles.__new__(recovery.SessionFiles)
        self.files.codex = {}
        self.files.roots = {'codex': self.root / 'codex', 'claude': self.root / 'claude'}

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self, content=b'{"message":"old conversation"}\n'):
        digest = hashlib.sha256(content).hexdigest()
        blob = self.store / 'blobs' / (digest + '.jsonl.gz')
        blob.parent.mkdir(exist_ok=True)
        blob.write_bytes(gzip.compress(content))
        return {'agent': 'codex', 'session_id': CODEX_ID,
                'path': f'sessions/2026/10/15/rollout-{CODEX_ID}.jsonl', 'sha256': digest}

    def test_resume_is_explicit_and_has_requested_permission_flags(self):
        self.assertEqual(recovery.resume_argv('codex', CODEX_ID),
                         ['codex', 'resume', CODEX_ID, '--dangerously-bypass-approvals-and-sandbox'])
        self.assertEqual(recovery.resume_argv('claude', CLAUDE_ID),
                         ['claude', '--resume', CLAUDE_ID, '--dangerously-skip-permissions'])

    def test_missing_ids_never_select_latest_session_in_shared_cwd(self):
        pane = {'agent': 'codex', 'pane_id': 'w1:p1', 'cwd': str(self.root),
                'terminal_title_stripped': 'Unknown | project'}
        self.files.codex[CODEX_ID] = {'cwd': str(self.root), 'name': 'Other conversation'}
        with self.assertRaises(recovery.RecoveryError):
            self.files.identify(pane, [])

    def test_ambiguous_codex_names_fail_even_with_old_resume_argument(self):
        pane = {'agent': 'codex', 'pane_id': 'w1:p1', 'cwd': str(self.root),
                'terminal_title_stripped': 'Same name | project'}
        for sid in [CODEX_ID, CLAUDE_ID]:
            self.files.codex[sid] = {'cwd': str(self.root), 'name': 'Same name'}
        with self.assertRaises(recovery.RecoveryError):
            self.files.identify(pane, [{'argv': ['codex', 'resume', CODEX_ID]}])

    def test_live_session_reference_wins_over_stale_launch_argument(self):
        pane = {'agent': 'codex', 'agent_session': {
            'agent': 'codex', 'kind': 'id', 'value': CLAUDE_ID}}
        self.assertEqual(self.files.identify(pane, [{'argv': ['codex', 'resume', CODEX_ID]}])[0],
                         CLAUDE_ID)

    def test_checksum_corruption_and_path_escape_fail_preflight(self):
        artifact = self.artifact()
        self.snapshot['artifacts'] = [artifact]
        recovery.validate_snapshot(self.snapshot, self.store)
        artifact['path'] = '../../auth.jsonl'
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_snapshot(self.snapshot, self.store)
        artifact['path'] = 'sessions/rollout.jsonl'
        artifact['sha256'] = '0' * 64
        (self.store / 'blobs' / ('0' * 64 + '.jsonl.gz')).write_bytes(gzip.compress(b'corrupt'))
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_snapshot(self.snapshot, self.store)

    def test_missing_transcripts_return_but_newer_local_work_is_preserved(self):
        artifact = self.artifact()
        self.snapshot['artifacts'] = [artifact]
        recovery.restore_transcripts(self.snapshot, self.files, self.store)
        path = self.files.roots['codex'] / artifact['path']
        self.assertEqual(path.read_bytes(), b'{"message":"old conversation"}\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_bytes(b'newer conversation\n')
        recovery.restore_transcripts(self.snapshot, self.files, self.store)
        self.assertEqual(path.read_bytes(), b'newer conversation\n')

    def test_running_session_is_refused_before_any_file_changes(self):
        snapshot = self.store / 'latest.json'
        snapshot.write_text(json.dumps(self.snapshot))
        original = self.root / 'session.json'
        original.write_text('existing state')
        args = argparse.Namespace(snapshot=snapshot, store=self.store, session=None,
                                  as_session=None, herdr='herdr', dry_run=False)
        with patch.object(recovery, 'herdr', return_value={'sessions': [
                {'name': 'default', 'default': True, 'running': True, 'session_dir': str(self.root)}]}):
            with self.assertRaises(recovery.RecoveryError):
                recovery.restore(args)
        self.assertEqual(original.read_text(), 'existing state')

    def test_stopped_herdr_does_not_overwrite_last_good_snapshot(self):
        latest = self.store / 'latest.json'
        latest.write_text('last good snapshot')
        with patch.object(recovery, 'herdr', return_value={'sessions': [{'running': False}]}):
            recovery.save(argparse.Namespace(store=self.store, herdr='herdr', keep=168))
        self.assertEqual(latest.read_text(), 'last good snapshot')

    def test_shell_layout_prevents_native_double_resume(self):
        state = self.snapshot['sessions'][0]['state']
        pane = state['workspaces'][0]['tabs'][0]['panes']['1']
        pane['agent_session'] = {'agent': 'codex', 'value': CODEX_ID}
        pane['launch_argv'] = ['codex']
        actual = recovery.shell_state(state)
        expected = copy.deepcopy(state)
        expected['workspaces'][0]['tabs'][0]['panes']['1'] = {'cwd': str(self.root)}
        self.assertEqual(actual, expected)
        self.assertIn('agent_session', pane)


@unittest.skipUnless(os.environ.get('HERDR_INTEGRATION_TEST') == '1',
                     'Set HERDR_INTEGRATION_TEST=1 to exercise a real isolated Herdr server.')
class HerdrIntegrationTests(unittest.TestCase):
    def test_real_restore_layout_and_agent_arguments(self):
        binary = shutil.which('herdr')
        self.assertIsNotNone(binary)
        # Keep Unix socket paths below macOS's 104-byte limit.
        with tempfile.TemporaryDirectory(prefix='hr-', dir='/tmp') as directory:
            root = Path(directory)
            cwd = root / 'project with spaces'
            cwd.mkdir()
            store = root / 'store'
            store.mkdir()
            snapshot = fixture(cwd)
            snapshot['sessions'][0]['state']['workspaces'][0]['tabs'][0]['zoomed'] = True
            recovery.write_json(store / 'latest.json', snapshot)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            markers = root / 'markers'
            markers.mkdir()
            stub = root / 'agent_stub.py'
            stub.write_text('import json,os,sys,time\nfrom pathlib import Path\n'
                            'p=Path(os.environ["TEST_MARKERS"])/'
                            '(os.environ["HERDR_PANE_ID"].replace(":","_")+".json")\n'
                            'p.write_text(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd()}))\n'
                            'time.sleep(3600)\n')
            import shlex
            for agent in ('codex', 'claude'):
                command = shlex.join([sys.executable, str(stub), agent])
                path = bin_dir / agent
                path.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
                path.chmod(0o755)
            config = root / 'config.toml'
            config.write_text('[terminal]\ndefault_shell = "/bin/bash"\nshell_mode = "non_login"\n'
                              '[update]\nversion_check = false\nmanifest_check = false\n')
            env = dict(os.environ, XDG_CONFIG_HOME=str(root / 'config'),
                       XDG_STATE_HOME=str(root / 'state'), HERDR_CONFIG_PATH=str(config),
                       CODEX_HOME=str(root / 'codex'), CLAUDE_CONFIG_DIR=str(root / 'claude'),
                       TEST_MARKERS=str(markers), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
            with patch.dict(os.environ, env, clear=True):
                try:
                    recovery.restore(argparse.Namespace(store=store, snapshot=None,
                                     session='default', as_session='recovery-test', herdr=binary,
                                     dry_run=False))
                    live = recovery.herdr(binary, 'api', 'snapshot',
                                          session='recovery-test')['result']['snapshot']
                    self.assertEqual([w['label'] for w in live['workspaces']],
                                     ['Development', 'Operations'])
                    self.assertEqual([t['label'] for t in live['tabs']], ['Agents', 'Shell', 'Console'])
                    self.assertEqual(len(live['panes']), 4)
                    layout = live['layouts'][0]
                    self.assertTrue(layout['zoomed'])
                    self.assertEqual(layout['splits'][0]['direction'], 'right')
                    self.assertAlmostEqual(layout['splits'][0]['ratio'], 0.35, places=5)
                    deadline = time.monotonic() + 5
                    while len(list(markers.glob('*.json'))) != 2 and time.monotonic() < deadline:
                        time.sleep(0.1)
                    launched = [json.loads(p.read_text()) for p in markers.glob('*.json')]
                    self.assertCountEqual([p['argv'] for p in launched], [
                        recovery.resume_argv('codex', CODEX_ID),
                        recovery.resume_argv('claude', CLAUDE_ID)])
                    self.assertEqual({str(Path(p['cwd']).resolve()) for p in launched},
                                     {str(cwd.resolve())})
                finally:
                    # This stops only the server created in the temporary XDG directory.
                    subprocess.run([binary, 'session', 'stop', 'recovery-test'],
                                   env=recovery.cli_env(), capture_output=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
