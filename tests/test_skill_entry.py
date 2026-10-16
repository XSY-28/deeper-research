"""Exercise the thin launcher against the real CLI in isolated workspaces."""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO/'skills/deeper-research/scripts/deeper.py'


class SkillEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)/'研究项目 with spaces'
        self.project.mkdir()
        self.other=Path(self.tmp.name)/'other-cwd';self.other.mkdir()
        config=json.loads((REPO/'src/deeper_research/defaults.json').read_text())
        for p in config['profiles'].values():
            p['api_key_env']='DEEPER_SKILL_TEST_UNCONFIGURED_CREDENTIAL'
        config['profiles']['different']=dict(config['profiles']['deepseek'],model='other-model',
                                              input_usd_per_million='0.9',output_usd_per_million='2.1')
        (self.project/'config.json').write_text(json.dumps(config))
        cli=self.project/'.venv/bin/deeper';cli.parent.mkdir(parents=True)
        cli.write_text('#!/usr/bin/env python3\nfrom deeper_research.cli import main\nraise SystemExit(main())\n')
        cli.chmod(0o755)
        self.env={**os.environ,'DEEPER_RESEARCH_ROOT':str(self.project),
                  'PYTHONPATH':str(REPO/'src'),'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')}
        self.env.pop('DEEPER_SKILL_TEST_UNCONFIGURED_CREDENTIAL',None)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self,*args,launcher=LAUNCHER,env=None):
        return subprocess.run([sys.executable,str(launcher),*args],cwd=self.other,
                              env=env or self.env,text=True,capture_output=True)

    def import_source(self):
        path=self.other/'source.md';path.write_text('# Evidence\n\nWeights remain frozen.\n')
        result=self.call('import',str(path),'--title','Test source')
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)['source_id']

    def test_other_cwd_keeps_the_programs_workspace_and_budget(self):
        first=self.call('budget');self.assertEqual(first.returncode,0,first.stderr)
        source=self.import_source()
        task=self.call('create','--question','What is supported?','--criterion','Cite the source',
                       '--source',source,'--effort','medium')
        self.assertEqual(task.returncode,0,task.stderr)
        current=json.loads(self.call('budget').stdout)
        self.assertEqual(current,json.loads(first.stdout))
        self.assertTrue((self.project/'workspace/state.sqlite').exists())
        self.assertFalse((self.other/'workspace').exists())

    def test_question_is_literal_data_without_shell_interpretation(self):
        source=self.import_source()
        question='核对 "引号"、$(touch SHOULD_NOT_EXIST) 和 `touch SHOULD_NOT_EXIST`；保留原文。'
        result=self.call('create','--question',question,'--criterion','Keep question literal',
                         '--source',source)
        self.assertEqual(result.returncode,0,result.stderr)
        task=json.loads(result.stdout)['task_id']
        shown=self.call('show',task)
        self.assertEqual(json.loads(shown.stdout)['spec']['question'],question)
        self.assertFalse((self.project/'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.other/'SHOULD_NOT_EXIST').exists())

    def test_profile_switch_is_frozen_by_core_without_changing_old_task(self):
        source=self.import_source()
        created=self.call('create','--question','Question','--criterion','Use source','--source',source)
        old=json.loads(created.stdout)['task_id']
        fork=self.call('fork',old,'--profile','different','--effort','high')
        self.assertEqual(fork.returncode,0,fork.stderr)
        new=json.loads(fork.stdout)['task_id']
        old_spec=json.loads(self.call('show',old).stdout)['spec']
        new_spec=json.loads(self.call('show',new).stdout)['spec']
        self.assertEqual(old_spec['profile']['model'],'deepseek-flash')
        self.assertEqual(new_spec['profile']['model'],'other-model')
        self.assertEqual(new_spec['parent_task_id'],old)
        self.assertEqual(new_spec['profile']['input_usd_per_million'],'0.9')
        self.assertEqual(json.loads(self.call('budget').stdout)['accounted_usd'],'0')

    def test_run_exit_code_and_missing_credentials_come_from_core(self):
        source=self.import_source()
        created=self.call('create','--question','Question','--criterion','Use source','--source',source)
        task=json.loads(created.stdout)['task_id']
        result=self.call('run',task)
        self.assertEqual(result.returncode,1)
        direct=subprocess.run([str(self.project/'.venv/bin/deeper'),'--config',str(self.project/'config.json'),
                               'run',task],cwd=self.project,env=self.env,text=True,capture_output=True)
        self.assertEqual((result.returncode,result.stderr),(direct.returncode,direct.stderr))
        with closing(sqlite3.connect(self.project/'workspace/state.sqlite')) as db:
            self.assertEqual(db.execute('select count(*) from calls').fetchone()[0],0)
        self.assertEqual(json.loads(self.call('budget').stdout)['accounted_usd'],'0')

    def test_symlinked_skill_resolves_its_physical_project(self):
        skill=self.project/'skills/deeper-research/scripts'
        skill.mkdir(parents=True)
        copied=skill/'deeper.py';shutil.copy2(LAUNCHER,copied)
        link=self.other/'installed-skill';link.symlink_to(skill.parent,target_is_directory=True)
        env=dict(self.env);env.pop('DEEPER_RESEARCH_ROOT')
        result=self.call('budget',launcher=link/'scripts/deeper.py',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.project/'workspace/state.sqlite').exists())
        self.assertFalse((self.other/'workspace').exists())

    def test_missing_project_fails_without_creating_a_new_ledger(self):
        env=dict(self.env);env['DEEPER_RESEARCH_ROOT']=str(self.other/'missing')
        result=self.call('budget',env=env)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('project',result.stderr.lower())
        self.assertFalse((self.other/'missing').exists())
