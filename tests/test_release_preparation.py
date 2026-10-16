"""Public exports use an explicit list; all secret fixtures are synthetic."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/prepare_release.py'


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        (self.root/'README.md').write_text('A small public CLI.\n')
        self.output=Path(self.tmp.name)/'public'

    def run_tool(self,files,output=True):
        (self.root/'release-files.json').write_text(json.dumps({'files':files}))
        args=[sys.executable,str(SCRIPT),'--root',str(self.root)]
        args+=['--output',str(self.output)] if output else ['--check']
        return subprocess.run(args,text=True,capture_output=True)

    def test_export_copies_only_listed_files_without_reading_private_env(self):
        (self.root/'.env').write_bytes(b'\xffprivate-unreadable-as-text')
        (self.root/'config.json').write_text('private config')
        result=self.run_tool(['README.md'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual([p.name for p in self.output.iterdir()],['README.md'])
        self.assertEqual((self.output/'README.md').read_bytes(),(self.root/'README.md').read_bytes())

    def test_private_paths_cannot_be_added_to_public_list(self):
        for name in ('.env','config.json','workspace/state.sqlite','.work/result.json','evals/run.json','docs/CONTINUE_HANDOFF.md','task_plan.md','notes.md'):
            with self.subTest(name=name):
                path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('not public')
                result=self.run_tool(['README.md',name]);self.assertNotEqual(result.returncode,0);self.assertIn('private-path',result.stderr)
                self.assertFalse(self.output.exists())

    def test_traversal_and_duplicate_manifest_entries_are_rejected(self):
        for files in (['../outside'],['/tmp/outside'],['README.md','README.md']):
            result=self.run_tool(files);self.assertNotEqual(result.returncode,0);self.assertIn('invalid-manifest',result.stderr);self.assertFalse(self.output.exists())

    def test_symlink_source_is_rejected_before_export(self):
        outside=self.root.parent/'private';outside.write_text('private target')
        (self.root/'linked.md').symlink_to(outside)
        result=self.run_tool(['linked.md']);self.assertNotEqual(result.returncode,0);self.assertIn('symlink',result.stderr);self.assertFalse(self.output.exists())

    def test_detected_token_is_never_echoed_in_failure_output(self):
        fake='sk-'+('A'*48)
        (self.root/'README.md').write_text('token='+fake)
        result=self.run_tool(['README.md']);self.assertNotEqual(result.returncode,0)
        self.assertIn('token',result.stderr)
        self.assertNotIn(fake,result.stdout+result.stderr);self.assertFalse(self.output.exists())

    def test_personal_home_paths_are_rejected_without_echoing_them(self):
        private='/'+'Users/example-private-person/Documents/research'
        (self.root/'README.md').write_text(private)
        result=self.run_tool(['README.md']);self.assertNotEqual(result.returncode,0)
        self.assertIn('personal-path',result.stderr)
        self.assertNotIn(private,result.stdout+result.stderr)

    def test_existing_destination_is_not_overwritten(self):
        self.output.mkdir();(self.output/'keep').write_text('keep')
        result=self.run_tool(['README.md']);self.assertNotEqual(result.returncode,0)
        self.assertIn('destination-exists',result.stderr)
        self.assertEqual((self.output/'keep').read_text(),'keep')

    def test_safe_example_env_and_check_mode_create_no_export(self):
        (self.root/'.env.example').write_text('DEEPSEEK_API_KEY=replace-with-your-local-key\n')
        result=self.run_tool(['README.md','.env.example'],output=False)
        self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(self.output.exists())

    def test_programmatically_constructed_dummy_assignment_is_not_a_literal_secret(self):
        (self.root/'sample.py').write_text("line='DEEPSEEK_API_KEY='+dummy.decode()+'\\n'\n")
        result=self.run_tool(['sample.py'],output=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_nonstandard_credential_literal_is_rejected_without_echoing_it(self):
        fake='opaque-auth-value-123456'
        (self.root/'.env.example').write_text('DEEPSEEK_API_KEY='+fake+'\n')
        result=self.run_tool(['.env.example']);self.assertNotEqual(result.returncode,0)
        self.assertIn('credential-literal',result.stderr);self.assertNotIn(fake,result.stderr+result.stdout)

    def test_private_path_checks_are_case_insensitive(self):
        for name in ('Workspace/data.json','CONFIG.JSON','.ENV','Evals/private.json'):
            with self.subTest(name=name):
                p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('not public')
                result=self.run_tool(['README.md',name]);self.assertNotEqual(result.returncode,0)
                self.assertIn('private-path',result.stderr);self.assertFalse(self.output.exists())

    def test_manifest_symlink_is_rejected_before_it_can_select_files(self):
        private=self.root/'.env';private.write_text(json.dumps({'files':['README.md']}))
        (self.root/'release-files.json').symlink_to(private)
        result=subprocess.run([sys.executable,str(SCRIPT),'--root',str(self.root),'--check'],text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('symlink',result.stderr)


if __name__=='__main__':unittest.main()
