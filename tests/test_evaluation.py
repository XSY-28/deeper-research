import json
from pathlib import Path
import tempfile
import unittest
import hashlib
from deeper_research.corpus import Corpus
from deeper_research.providers import ProviderProfile, ModelResult
from deeper_research.evaluation import run_evaluation

from deeper_research.evaluation import load_manifest, export_blind
from deeper_research.storage import Store


class EvaluationTests(unittest.TestCase):
    def test_blind_export_can_include_initial_draft_without_revealing_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); store=Store(root/'work');corpus=Corpus(root/'work')
            original=root/'source.md';original.write_text('Original source evidence.')
            source=corpus.ingest(original)
            task=store.create_task({'question':'Question','criteria':['Evidence'],'source_ids':[source],'effort':'high','profile':{}})
            first=store.save_artifact(task,'candidate',{'answer':'First draft','claims':[],'open_issues':[]})
            final=store.save_artifact(task,'candidate',{'answer':'Final answer','claims':[],'open_issues':[]})
            store.update(task,selected=final,state='finished',checkpoint={'initial':first})
            run=root/'run.json';run.write_text(json.dumps({'runs':[{'case_id':'case','task_id':task,'effort':'high'}]}))
            result=export_blind(store,run,root/'blind',include_initial=True)
            mapping=json.loads(Path(result['mapping']).read_text())
            self.assertEqual({r['version'] for r in mapping},{'initial','selected'})
            self.assertEqual(result['count'],2)
            for p in (root/'blind'/'answers').glob('*.md'):
                self.assertNotIn('version',p.read_text())
                self.assertNotIn(task,p.read_text())

    def test_blind_export_includes_scoped_sources_and_criteria_without_run_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); store=Store(root/'work'); corpus=Corpus(root/'work')
            original=root/'source.md';original.write_text('The method needs external evidence.\n')
            source=corpus.ingest(original,title='Primary source',url='https://example.org/paper')
            block=corpus.search('external',[source])[0]
            task=store.create_task({'question':'What does the method require?','criteria':['Use the original evidence'],
                                   'source_ids':[source],'effort':'high','profile':{}})
            candidate=store.save_artifact(task,'candidate',{'answer':'External evidence is required.',
                'claims':[{'text':'External evidence is required','evidence_ids':[block['id']]}],'open_issues':[]})
            store.update(task,selected=candidate,state='finished',quality='partial')
            run=root/'run.json';run.write_text(json.dumps({'runs':[{'case_id':'case','task_id':task,'effort':'high'}]}))
            result=export_blind(store,run,root/'blind')
            sources=json.loads(Path(result['sources']).read_text())
            self.assertEqual(sources[block['id']]['location'],{'line_start':1,'line_end':1})
            self.assertIn('needs external evidence',sources[block['id']]['text'])
            cases=json.loads(Path(result['criteria']).read_text())
            self.assertEqual(cases[0]['criteria'],['Use the original evidence'])
            self.assertNotIn('effort',json.dumps(cases))
            self.assertNotIn(task,json.dumps(cases))
            self.assertEqual(cases[0]['source_ids'],[source])
            catalog=json.loads(Path(result['source_catalog']).read_text())
            self.assertEqual(catalog[source]['url'],'https://example.org/paper')
            snapshot=Path(result['source_catalog']).parent/catalog[source]['snapshot_path']
            self.assertEqual(snapshot.read_bytes(),original.read_bytes())

    def test_invalid_model_answer_is_recorded_without_dropping_later_cases(self):
        class Remote:
            profile = ProviderProfile.from_dict({"protocol":"openai_chat", "model":"test", "base_url":"https://example.com/v1", "api_key_env":"TEST_KEY", "input_usd_per_million":"0.3", "output_usd_per_million":"1.2"})
            def __init__(self): self.index=0
            def validate_credentials(self): pass
            def complete(self,messages,*,max_output_tokens,timeout):
                self.index+=1
                value = {} if self.index == 1 else {"answer":"Evidence [E1]", "claims":[{"text":"Evidence", "evidence_ids":["E1"]}],"open_issues":[]}
                return ModelResult(json.dumps(value),10,10,20,"r","test",{})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'s.md';source.write_text('Evidence supports a statement.')
            data={"version":1,"sources":{"s":{"path":"s.md","title":"Evidence","url":"https://example.com","sha256":hashlib.sha256(source.read_bytes()).hexdigest()}},"development":[],"evaluation":[{"id":f"case-{i}","question":"Evidence?","criteria":["Cite evidence"],"sources":["s"]} for i in range(2)]}
            manifest=root/'manifest.json';manifest.write_text(json.dumps(data))
            store=Store(root/'work');corpus=Corpus(root/'work')
            path=run_evaluation(store,corpus,Remote(),manifest,root,efforts=['low'])
            runs=json.loads(path.read_text())['runs']
            self.assertEqual([r['state'] for r in runs], ['failed','finished'])
    def test_manifest_rejects_modified_source_before_solving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"source.md").write_text("changed")
            manifest = {"version": 1, "sources": {"s": {"path": "source.md", "sha256": "0"*64}},
                        "development": [], "evaluation": []}
            (root/"manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                load_manifest(root/"manifest.json", root)

    def test_blind_export_separates_mapping_from_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root/"work")
            source_file=root/'evidence.md';source_file.write_text('Evidence for evaluation.')
            source_id=Corpus(root/'work').ingest(source_file)
            task = store.create_task({"question": "Question", "criteria": ["Evidence"], "source_ids": [source_id],
                                      "effort": "high", "profile": {}})
            path = store.save_artifact(task, "candidate", {"answer": "A supported answer", "claims": [], "open_issues": []})
            store.update(task, selected=path, state="finished", quality="partial")
            run = root/"run.json"
            run.write_text(json.dumps({"runs": [{"case_id": "case", "task_id": task, "effort": "high"}]}))
            result = export_blind(store, run, root/"blind")
            answers = list((root/"blind"/"answers").glob("*.md"))
            self.assertEqual(len(answers), 1)
            self.assertIn("A supported answer", answers[0].read_text())
            self.assertNotIn(task, answers[0].read_text())
            self.assertNotIn("high", answers[0].read_text())
            self.assertTrue(Path(result["mapping"]).exists())


if __name__ == "__main__":
    unittest.main()
