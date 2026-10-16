import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
        self.env.pop("DEEPSEEK_API_KEY", None)
    def tearDown(self):
        self.tmp.cleanup()
    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "deeper_research", *args],
                              cwd=self.root, env=self.env, capture_output=True, text=True)
    def test_import_create_show_do_not_require_credentials(self):
        init = self.cli("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        source = self.root / "source.md"
        source.write_text("# Evidence\n\nThe method has limited evidence.\n")
        imported = self.cli("import", str(source))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        source_id = json.loads(imported.stdout)["source_id"]
        created = self.cli("create", "--question", "What evidence?", "--criterion", "Cite source",
                           "--source", source_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        task_id = json.loads(created.stdout)["task_id"]
        shown = self.cli("show", task_id)
        self.assertEqual(json.loads(shown.stdout)["state"], "ready")
        failed = self.cli("run", task_id)
        self.assertNotEqual(failed.returncode, 0)
        budget = self.cli("budget")
        self.assertEqual(json.loads(budget.stdout)["accounted_usd"], "0")
    def test_init_never_overwrites_existing_config(self):
        self.cli("init")
        p = self.root / "config.json"
        data = json.loads(p.read_text())
        data["project_budget_usd"] = "0.01"
        p.write_text(json.dumps(data))
        self.cli("init")
        self.assertEqual(json.loads(p.read_text())["project_budget_usd"], "0.01")


if __name__ == "__main__":
    unittest.main()

class MultiModeCLITests(unittest.TestCase):
    setUp=CLITests.setUp
    tearDown=CLITests.tearDown
    cli=CLITests.cli
    def test_multi_and_equal_budget_single_are_prepared_without_credentials(self):
        self.cli('init')
        source=self.root/'source.md';source.write_text('Evidence for the question.')
        sid=json.loads(self.cli('import',str(source)).stdout)['source_id']
        for mode in ('multi','single-budgeted'):
            created=self.cli('create','--question','What evidence?','--criterion','Cite source',
                '--source',sid,'--mode',mode,'--task-budget','0.25')
            self.assertEqual(created.returncode,0,created.stderr)
            tid=json.loads(created.stdout)['task_id'];task=json.loads(self.cli('show',tid).stdout)
            self.assertEqual((task['spec']['mode'],task['spec']['effort'],task['spec']['budget_usd']),(mode,'high','0.25'))
            self.assertEqual((task['spec']['limits']['calls'],task['spec']['limits']['tokens']),(60,640000))
            failed=self.cli('run',tid)
            self.assertNotEqual(failed.returncode,0)
            self.assertEqual(json.loads(self.cli('budget').stdout)['accounted_usd'],'0')
