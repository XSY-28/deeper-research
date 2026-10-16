"""First-use setup with TEMP files and dummy credentials; network prohibited."""
import getpass
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout,redirect_stderr,closing
from deeper_research.cli import main,load_env


class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=self.root/'config.json'
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)

    def run_init(self,inputs=(),secret='dummy-setup-secret',tty=True,flags=()):
        out,err=io.StringIO(),io.StringIO()
        with patch('sys.stdin.isatty',return_value=tty),patch('builtins.input',side_effect=inputs),\
             patch('getpass.getpass',side_effect=secret if isinstance(secret,(list,BaseException)) else None,return_value=secret),\
             patch('deeper_research.providers.urlopen',side_effect=AssertionError('Network forbidden')),\
             redirect_stdout(out),redirect_stderr(err):
            try:code=main(['--config',str(self.config),'init',*flags])
            except SystemExit as ex:code=ex.code
        return code,out.getvalue(),err.getvalue()

    def seed(self):
        code,_,err=self.run_init(tty=False);self.assertEqual(code,0,err)
        return json.loads(self.config.read_text())

    def test_first_tty_init_saves_hidden_key_separately_with_private_permissions(self):
        code,out,err=self.run_init(['','y'])
        self.assertEqual(code,0,err);self.assertTrue((self.root/'.env').is_file())
        self.assertEqual(stat.S_IMODE((self.root/'.env').stat().st_mode),0o600)
        load_env(self.root/'.env',{'DEEPSEEK_API_KEY'})
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'dummy-setup-secret')
        self.assertNotIn('dummy-setup-secret',out+err+self.config.read_text())
        self.assertEqual(json.loads(out)['budget']['accounted_usd'],'0')

    def test_non_tty_init_stays_noninteractive_and_creates_no_secret_file(self):
        code,_,err=self.run_init(tty=False)
        self.assertEqual(code,0,err);self.assertFalse((self.root/'.env').exists())

    def test_explicit_noninteractive_flag_skips_tty_prompts(self):
        code,_,err=self.run_init(tty=True,flags=['--no-interactive'])
        self.assertEqual(code,0,err);self.assertFalse((self.root/'.env').exists())

    def test_forced_interactive_without_tty_refuses_before_writing(self):
        code,_,_=self.run_init(tty=False,flags=['--interactive'])
        self.assertEqual(code,1);self.assertFalse(self.config.exists());self.assertFalse((self.root/'workspace').exists())

    def test_existing_key_is_preserved_by_default_without_secret_prompt(self):
        self.seed();env=self.root/'.env';env.write_text('# Keep me\nDEEPSEEK_API_KEY=dummy-existing\nOTHER=unchanged\n')
        before=env.read_bytes();config_before=self.config.read_bytes()
        code,out,err=self.run_init(['','','y'],secret=AssertionError('Must not ask for secret'),flags=['--interactive'])
        self.assertEqual(code,0,err);self.assertEqual(env.read_bytes(),before);self.assertEqual(self.config.read_bytes(),config_before)
        self.assertNotIn('dummy-existing',out+err)

    def test_confirmed_key_replacement_preserves_other_settings_and_loads_new_value(self):
        self.seed();env=self.root/'.env';env.write_text('# Keep me\nDEEPSEEK_API_KEY=dummy-old\nOTHER=unchanged\nDEEPSEEK_API_KEY=dummy-duplicate\n')
        code,out,err=self.run_init(['','y','y'],secret='dummy-replaced',flags=['--interactive'])
        self.assertEqual(code,0,err);load_env(env,{'DEEPSEEK_API_KEY'})
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'dummy-replaced')
        self.assertIn('# Keep me\n',env.read_text());self.assertIn('OTHER=unchanged\n',env.read_text())
        self.assertEqual(env.read_text().count('DEEPSEEK_API_KEY='),1)
        self.assertEqual(stat.S_IMODE(env.stat().st_mode),0o600);self.assertNotIn('dummy-replaced',out+err)

    def test_profile_selection_changes_only_default_and_does_not_reset_ledger(self):
        before=self.seed();before['project_budget_usd']='0.25';self.config.write_text(json.dumps(before))
        code,_,err=self.run_init(['2','y'],secret='dummy-openai',flags=['--interactive'])
        self.assertEqual(code,0,err);after=json.loads(self.config.read_text())
        self.assertEqual(after,{**before,'default_profile':'openai_chat'})
        load_env(self.root/'.env',{'OPENAI_API_KEY'});self.assertEqual(os.environ['OPENAI_API_KEY'],'dummy-openai')
        self.assertEqual(json.loads(self.run_init(tty=False)[1])['budget']['limit_usd'],'10.00')

    def test_cancel_before_save_creates_no_config_secret_or_workspace(self):
        code,_,_=self.run_init(['','n'])
        self.assertEqual(code,130);self.assertEqual(list(self.root.iterdir()),[])

    def test_interrupted_secret_entry_creates_no_files(self):
        code,_,_=self.run_init([''],secret=KeyboardInterrupt())
        self.assertEqual(code,130);self.assertEqual(list(self.root.iterdir()),[])

    def test_secret_entry_without_echo_protection_is_refused(self):
        code,_,err=self.run_init([''],secret=getpass.GetPassWarning('dummy warning'))
        self.assertEqual(code,1);self.assertEqual(list(self.root.iterdir()),[])
        self.assertNotIn('dummy warning',err)

    def test_invalid_secret_is_not_echoed_and_can_be_reentered(self):
        code,out,err=self.run_init(['','y'],secret=['invalid\nheader','dummy-valid'])
        self.assertEqual(code,0,err);self.assertNotIn('invalid\nheader',out+err)
        self.assertTrue((self.root/'.env').is_file())
        load_env(self.root/'.env',{'DEEPSEEK_API_KEY'});self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'dummy-valid')

    def test_existing_environment_is_kept_without_writing_a_shadow_key(self):
        os.environ['DEEPSEEK_API_KEY']='dummy-in-environment'
        code,out,err=self.run_init(['','','y'],secret=AssertionError('No secret prompt'))
        self.assertEqual(code,0,err);self.assertFalse((self.root/'.env').exists())
        self.assertNotIn('dummy-in-environment',out+err)

    def test_secret_symlink_is_rejected_without_touching_target(self):
        target=self.root/'outside';target.write_text('untouched')
        (self.root/'.env').symlink_to(target)
        code,_,_=self.run_init(['','y'])
        self.assertEqual(code,1);self.assertEqual(target.read_text(),'untouched');self.assertFalse(self.config.exists())

    def test_key_save_failure_keeps_first_init_recoverable_and_explains_retry(self):
        import deeper_research.setup_wizard as wizard
        replace=wizard.os.replace
        def fail_key(src,dst):
            if Path(dst).name=='.env':raise PermissionError('simulated local write failure')
            return replace(src,dst)
        with patch.object(wizard.os,'replace',side_effect=fail_key):code,out,err=self.run_init(['','y'])
        self.assertEqual(code,1);self.assertFalse(self.config.exists());self.assertFalse((self.root/'.env').exists())
        self.assertIn('init --interactive',err);self.assertNotIn('dummy-setup-secret',out+err)
        self.assertEqual(list(self.root.glob('.env.*.tmp')),[])

    def test_interrupted_commit_reports_recovery_instead_of_pre_save_cancel(self):
        import deeper_research.setup_wizard as wizard
        with patch.object(wizard.os,'replace',side_effect=KeyboardInterrupt()):code,_,err=self.run_init(['','y'])
        self.assertEqual(code,1);self.assertIn('init --interactive',err)
        self.assertFalse(self.config.exists());self.assertFalse((self.root/'workspace').exists())

    def test_config_save_failure_reports_possible_partial_save_without_leaking_key(self):
        import deeper_research.setup_wizard as wizard
        replace=wizard.os.replace
        def fail_config(src,dst):
            if Path(dst).resolve()==self.config.resolve():raise PermissionError('simulated config failure')
            return replace(src,dst)
        with patch.object(wizard.os,'replace',side_effect=fail_config):code,out,err=self.run_init(['','y'])
        self.assertEqual(code,1);self.assertIn('init --interactive',err)
        self.assertNotIn('dummy-setup-secret',out+err);self.assertFalse(self.config.exists())

    def test_reconfiguration_preserves_nonzero_existing_call_history_and_budget(self):
        from decimal import Decimal
        import sqlite3
        from deeper_research.storage import Store
        self.seed();store=Store(self.root/'workspace')
        profile=json.loads(self.config.read_text())['profiles']['deepseek']
        tid=store.create_task({'question':'Dummy task','criteria':['Cite evidence'],'source_ids':['s1'],'effort':'high','profile':profile})
        cid=store.reserve_call(tid,'solve',[],100,100,Decimal('.30'),Decimal('1.20'))
        store.fail_call(cid,'TEMP dummy unknown outcome',unknown=True)
        def snapshot():
            with closing(sqlite3.connect(store.db_path)) as db:
                return {t:db.execute('select * from '+t+' order by rowid').fetchall() for t in ('settings','tasks','calls','events','artifacts','task_groups')}
        before=snapshot();budget=store.budget();self.assertGreater(Decimal(budget['accounted_usd']),0)
        code,_,err=self.run_init(['2','y'],secret='dummy-openai',flags=['--interactive'])
        self.assertEqual(code,0,err);self.assertEqual(snapshot(),before);self.assertEqual(store.budget(),budget)

    @unittest.skipUnless(os.name=='posix','CLI currently requires POSIX file locking')
    def test_real_terminal_first_use_hides_keystrokes_and_never_calls_network(self):
        import errno,pty,select,signal,sys,time
        dummy=b'dummy-pty-only-secret'
        repo=Path(__file__).resolve().parents[1]
        script=("from unittest.mock import patch\nfrom deeper_research.cli import main\n"
                "with patch('deeper_research.providers.urlopen',side_effect=AssertionError('Network forbidden')):\n"
                " raise SystemExit(main(['init']))\n")
        pid,fd=pty.fork()
        if pid==0:
            os.chdir(self.root)
            os.execve(sys.executable,[sys.executable,'-c',script],
                {'PYTHONPATH':str(repo/'src'),'PYTHONUTF8':'1','TERM':'xterm'})
        captured=b'';deadline=time.monotonic()+20;finished=False
        def read_more():
            nonlocal captured
            self.assertLess(time.monotonic(),deadline,'TTY setup did not complete')
            if not select.select([fd],[],[],.2)[0]:return True
            try:data=os.read(fd,65536)
            except OSError as ex:
                if ex.errno==errno.EIO:return False
                raise
            captured+=data;return bool(data)
        def until(marker):
            while marker not in captured:
                self.assertTrue(read_more(),'TTY exited before expected prompt')
        try:
            until('选择profile'.encode());os.write(fd,b'\n')
            until('DEEPSEEK_API_KEY（隐藏输入，回车跳过）：'.encode());os.write(fd,dummy+b'\n')
            until('保存这些本地设置？'.encode());os.write(fd,b'y\n')
            while read_more():pass
            _,status=os.waitpid(pid,0);finished=True
            self.assertEqual(os.waitstatus_to_exitcode(status),0,captured.decode(errors='replace'))
            self.assertNotIn(dummy,captured)
            self.assertEqual((self.root/'.env').read_text(),'DEEPSEEK_API_KEY='+dummy.decode()+'\n')
            self.assertEqual(stat.S_IMODE((self.root/'.env').stat().st_mode),0o600)
        finally:
            os.close(fd)
            if not finished:
                try:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
