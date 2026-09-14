"""Run the real playbooks against fake CLIs in an isolated temporary environment."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
import yaml
name = Path(sys.argv[0]).name
a = sys.argv[1:]
base = Path(os.environ['FAKE_ROOT'])
state_path = base / 'state.json'
s = json.loads(state_path.read_text()) if state_path.exists() else {'resources': {}}
stdin = sys.stdin.read() if '-f' in a and '-' in a else ''
with (base / 'calls.jsonl').open('a') as f:
 f.write(json.dumps({'cli': name, 'args': a}) + '\n')
def output(v=''):
 print(v)
 state_path.write_text(json.dumps(s))
 sys.exit(0)
def option(k):
 return a[a.index(k)+1]
if name == 'docker': output('Docker available')
if name == 'skupper':
 if a == ['version']: output('2.2.1')
 ns = option('-n') if '-n' in a else 'default'
 remote = Path(os.environ['XDG_DATA_HOME']) / 'skupper/namespaces' / ns
 if a[:2] == ['token', 'issue']:
  Path(a[2]).write_text('secret test token')
  output('Issued')
 if a[:2] == ['token', 'redeem']:
  if os.environ.get('FAIL_REDEEM'):
   print('simulated redemption failure', file=sys.stderr); sys.exit(1)
  (remote / 'input/resources/Link-test.yaml').write_text('kind: Link\n')
  (remote / 'input/resources/Secret-test.yaml').write_text('kind: Secret\n')
  output('Redeemed')
 if a[:2] == ['system', 'start']:
  (remote / 'internal').mkdir(exist_ok=True)
  (remote / 'internal/platform.yaml').write_text('docker')
  output('Started')
 if a[:2] == ['system', 'stop']:
  (remote / 'internal/platform.yaml').unlink(missing_ok=True)
  output('Stopped')
 if a[:2] in (['system','install'], ['system','reload']): output('OK')
if name == 'oc':
 if a[:2] == ['whoami', '--show-server']: output(os.environ.get('FAKE_API', 'https://api.example.test:6443'))
 if a[0] == 'get':
  kind = a[1]
  if kind.startswith(('namespace/', 'crd/')): output(kind)
  if kind == 'sites': output(json.dumps({'items':[v for k,v in s['resources'].items() if k.startswith('site/')]}))
  if kind == 'site': output('2')
  if kind in s['resources']: output(json.dumps(s['resources'][kind]))
  if '--ignore-not-found' in a: output('')
 if a[0] in ('apply','delete') and stdin:
  for d in yaml.safe_load_all(stdin):
   key = d['kind'].lower() + '/' + d['metadata']['name']
   if a[0] == 'apply': s['resources'][key] = d
   else: s['resources'].pop(key,None)
  output('resource created' if a[0]=='apply' else 'resource deleted')
 if a[0] == 'create' and stdin:
  d=yaml.safe_load(stdin)
  assert d['kind']=='Pod' and 'generateName' in d['metadata']
  assert 'Authorization' not in stdin
  s['resources']['pod/model-routing-probe-test']=d
  output('pod/model-routing-probe-test')
 if a[0] == 'delete':
  for key in a[1:]: s['resources'].pop(key,None)
  output('resource deleted')
 if a[0] == 'wait' and a[1].startswith('pod/') and os.environ.get('FAIL_PROBE'):
  print('simulated failed probe',file=sys.stderr); sys.exit(1)
 if a[0] in ('rollout','wait'): output('ready')
 if a[0] == 'logs': output('HTTP 401')
print('Unexpected mock command: ' + repr([name]+a),file=sys.stderr)
sys.exit(2)
'''


class Playbooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='skupper-playbook-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'ansible'
        shutil.copytree(ROOT / 'ansible', self.work)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('oc', 'skupper', 'docker'):
            p = self.bin / name
            p.write_text(FAKE.replace('/usr/bin/python3', sys.executable, 1))
            p.chmod(0o755)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        config = yaml.safe_load((self.work / 'config.example.yml').read_text())
        config.update(cluster_api='https://api.example.test:6443', model_port=self.server.server_port,
                      remote_skupper_bin=str(self.bin / 'skupper'), local_skupper_bin=str(self.bin / 'skupper'))
        (self.work / 'config.yml').write_text(yaml.safe_dump(config))
        (self.work / 'inventory.ini').write_text(f'[model_hosts]\nmodel_host ansible_connection=local ansible_python_interpreter={sys.executable}\n\n[local]\nlocalhost ansible_connection=local ansible_python_interpreter={sys.executable}\n')
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        FAKE_ROOT=str(self.root), XDG_DATA_HOME=str(self.root / 'data'),
                        ANSIBLE_NOCOLOR='1', ANSIBLE_CONFIG=str(self.work / 'ansible.cfg'))
        self.remote = self.root / 'data/skupper/namespaces/skupper-model-routing'

    def run_play(self, play, ok=True, extra_env=None):
        result = subprocess.run(['ansible-playbook', play], cwd=self.work,
                                env=dict(self.env, **(extra_env or {})), capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def calls(self):
        p = self.root / 'calls.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def assert_tokens_removed(self):
        self.assertFalse((self.remote / '.link-token.yaml').exists())
        for c in self.calls():
            if c['args'][:2] == ['token', 'issue']:
                self.assertFalse(Path(c['args'][2]).parent.exists())

    def test_setup_repeat_verify_teardown(self):
        self.run_play('up.yml')
        self.assertTrue((self.remote / 'input/resources/Link-test.yaml').exists())
        self.assert_tokens_removed()
        self.run_play('up.yml')
        self.assert_tokens_removed()
        self.run_play('verify.yml')
        self.run_play('down.yml')
        self.run_play('down.yml')
        self.assertFalse(self.remote.exists())
        self.assertEqual(json.loads((self.root/'state.json').read_text())['resources'], {})
        for c in self.calls():
            self.assertNotIn('--all', c['args'])

    def test_failed_probe_is_removed(self):
        self.run_play('up.yml', ok=False, extra_env={'FAIL_PROBE':'1'})
        self.assert_tokens_removed()
        resources = json.loads((self.root/'state.json').read_text())['resources']
        self.assertFalse(any(k.startswith('pod/') for k in resources))

    def test_wrong_cluster_stops_before_mutation(self):
        r = self.run_play('down.yml', ok=False, extra_env={'FAKE_API':'https://wrong.example.test:6443'})
        self.assertIn('does not exactly match', r.stdout)
        self.assertEqual([c['args'] for c in self.calls()], [['whoami','--show-server']])

    def test_unowned_remote_is_preserved(self):
        self.remote.mkdir(parents=True)
        sentinel = self.remote / 'keep-me'
        sentinel.write_text('existing site')
        r = self.run_play('down.yml', ok=False)
        self.assertIn('without our ownership marker', r.stdout)
        self.assertTrue(sentinel.exists())
        self.assertFalse(any(c['args'][0] in ('apply','delete') for c in self.calls()))

    def test_unowned_cluster_site_is_preserved(self):
        state = {'resources': {'site/someone-else': {'kind':'Site','metadata': {'name':'someone-else'}}}}
        (self.root / 'state.json').write_text(json.dumps(state))
        r = self.run_play('up.yml', ok=False)
        self.assertIn('unrelated Skupper site', r.stdout)
        self.assertEqual(json.loads((self.root/'state.json').read_text()),state)

    def test_redemption_failure_cleans_both_tokens(self):
        self.run_play('up.yml', ok=False, extra_env={'FAIL_REDEEM':'1'})
        self.assert_tokens_removed()
        self.run_play('up.yml')
        self.run_play('down.yml')


if __name__ == '__main__':
    unittest.main()
