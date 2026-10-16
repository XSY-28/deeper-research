from contextlib import closing
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from deeper_research.contracts import estimate_tokens
from deeper_research.corpus import Corpus


def make_pdf(path, pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        if text is not None:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(text if isinstance(text, bytes) else
                            ('BT /F1 12 Tf 50 700 Td (' + text + ') Tj ET').encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.corpus = Corpus(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def text_file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8', newline='')
        return path

    def test_snapshot_survives_original_edits_and_reopening(self):
        path = self.text_file('note.txt', 'original evidence\n')
        first = self.corpus.ingest(path)
        block = self.corpus.search('original', [first])[0]
        path.write_text('new evidence\n')
        second = self.corpus.ingest(path)
        self.assertNotEqual(first, second)
        reopened = Corpus(self.workspace)
        self.assertEqual(reopened.get_block(block['id'], [first])['text'], 'original evidence\n')
        self.assertEqual(len(reopened.list_sources()), 2)
        self.assertEqual(reopened.ingest(path), second)
        digest = hashlib.sha256(b'original evidence\n').hexdigest()
        self.assertEqual((self.workspace / 'sources' / digest / 'original.txt').read_bytes(), b'original evidence\n')

    def test_markdown_exact_text_and_original_line_locations(self):
        path = self.text_file('notes.md', '# Header\r\n\r\nParagraph one\r\ncontinued\r\n\r\nlast line')
        source = self.corpus.ingest(path, title='Reading')
        block = self.corpus.search('Paragraph', [source])[0]
        self.assertEqual(block['text'], 'Paragraph one\r\ncontinued\r\n')
        self.assertEqual(block['location'], {'line_start': 3, 'line_end': 4})
        self.assertEqual(block['title'], 'Reading')

    def test_scope_never_falls_back_to_other_sources(self):
        a = self.corpus.ingest(self.text_file('a.txt', 'rareword alpha'))
        b = self.corpus.ingest(self.text_file('b.txt', 'rareword beta'))
        block = self.corpus.search('rareword', [a])[0]
        self.assertEqual(self.corpus.search('rareword', []), [])
        self.assertEqual(self.corpus.search('rareword', ['unknown']), [])
        self.assertEqual({r['source_id'] for r in self.corpus.search('rareword', [b])}, {b})
        for scope in ([], [b], ['unknown']):
            with self.assertRaises(KeyError):
                self.corpus.get_block(block['id'], scope)

    def test_chinese_terms_find_phrase_inside_unspaced_text(self):
        a = self.corpus.ingest(self.text_file('cn.txt', '研究在线学习算法的动态遗憾界。\n\n梯度下降更新参数。'))
        result = self.corpus.search('动态遗憾', [a])
        self.assertIn('动态遗憾', result[0]['text'])
        self.assertEqual(self.corpus.search('量子纠缠', [a]), [])

    def test_chinese_criterion_finds_english_stopping_rule_before_title_mentions(self):
        source = self.corpus.ingest(self.text_file('method.txt',
            'Method Overview. We introduce a method for language systems.\n\n'
            'Stopping: terminate at the iteration limit or a stop indicator from feedback.\n\n'
            'Related work. This method is compared with other language systems.'), title='Method')
        results = self.corpus.search('比较 Method 的终止条件与迭代上限', [source], limit=1)
        self.assertIn('terminate at the iteration limit', results[0]['text'])

    def test_specific_query_keeps_rare_concept_instead_of_front_page_name(self):
        text = 'Method retention background and overview. ' * 140 + '\n\n' + (
            'History retention: previous outputs and feedback are retained in the prompt. '
            'The history buffer has a maximum capacity.\n\n') + 'Method other detail. ' * 140
        path = self.root / 'history.pdf'
        make_pdf(path, [text])
        source = self.corpus.ingest(path, title='Method')
        result = self.corpus.search('Method history retention memory', [source], max_tokens=400)[0]
        self.assertIn('previous outputs and feedback are retained', result['text'])

    def test_expanded_query_preserves_scope_and_reports_retrieval_terms(self):
        allowed = self.corpus.ingest(self.text_file('allowed.txt', 'No stopping information here.'))
        other = self.corpus.ingest(self.text_file('other.txt', 'Terminate after the maximum iterations.'))
        results = self.corpus.search('终止条件', [allowed], max_tokens=500)
        self.assertTrue(results)
        self.assertEqual({r['source_id'] for r in results}, {allowed})
        self.assertIn('stopping', results[0]['query_terms'])

    def test_parameter_query_matches_plural_weights_before_unrelated_training(self):
        source=self.corpus.ingest(self.text_file('parameters.txt',
            'The parameters are frozen; the weights do not change.\n\n'
            'Gradient training updates an unrelated classification system.'))
        block=self.corpus.search('参数是否更新', [source], limit=1)[0]
        self.assertIn('weights do not change', block['text'])

    def test_complete_paragraph_preserves_negation_before_match(self):
        text = 'Background detail. ' * 200 + '\n\nThis compound is not safe for drinking. Do not use it.\n\n' + 'More detail. '*200
        source = self.corpus.ingest(self.text_file('safety.txt', text))
        block = self.corpus.search('safe', [source], max_tokens=350)[0]
        self.assertIn('not safe', block['text'])
        self.assertEqual(block['text'],'This compound is not safe for drinking. Do not use it.\n')

    def test_pdf_extractor_upgrade_creates_new_parse_version(self):
        path = self.root / 'versioned.pdf'
        make_pdf(path, ['Evidence'])
        first = self.corpus.ingest(path)
        with patch('pypdf.__version__', '999.0.test'):
            second = self.corpus.ingest(path)
        self.assertNotEqual(first, second)

    def test_long_first_source_does_not_consume_comparison_packet(self):
        a = self.corpus.ingest(self.text_file('alpha.txt', 'evidence alpha '*1500+'\n\nevidence alpha.'))
        b = self.corpus.ingest(self.text_file('beta.txt', 'evidence beta '*1500+'\n\nevidence beta.'))
        packet = self.corpus.search('evidence', [a,b], max_tokens=1600)
        self.assertEqual({x['source_id'] for x in packet}, {a,b})

    def test_pdf_physical_pages_and_warnings(self):
        path = self.root / 'paper.pdf'
        make_pdf(path, ['Page one evidence', None, 'Page three evidence'])
        source = self.corpus.ingest(path)
        blocks = self.corpus.search('evidence', [source])
        self.assertEqual({b['location']['page'] for b in blocks}, {1, 3})
        self.assertTrue(all(b['location']['bbox'] is None for b in blocks))
        metadata = self.corpus.list_sources()[0]
        self.assertTrue(any('2' in w and 'empty' in w.lower() for w in metadata['warnings']))
        self.assertTrue(any('equations' in w and 'tables' in w for w in metadata['warnings']))

    def test_failed_extraction_preserves_previous_source(self):
        path = self.root / 'paper.pdf'
        make_pdf(path, ['Valid evidence'])
        first = self.corpus.ingest(path)
        block = self.corpus.search('Valid', [first])[0]
        path.write_bytes(b'not a pdf')
        with self.assertRaises(ValueError):
            self.corpus.ingest(path)
        self.assertEqual([s['id'] for s in self.corpus.list_sources()], [first])
        self.assertIn('Valid', self.corpus.get_block(block['id'], [first])['text'])

    def test_page_extraction_failure_is_warning_with_physical_page(self):
        path = self.root / 'partial.pdf'
        make_pdf(path, ['Good evidence', b'BT (unterminated', 'Third evidence'])
        source = self.corpus.ingest(path)
        blocks = self.corpus.search('evidence', [source])
        self.assertEqual({b['location']['page'] for b in blocks}, {1, 3})
        self.assertTrue(any('Page 2' in w and 'failed' in w
                            for w in self.corpus.list_sources()[0]['warnings']))

    def test_corrupt_immutable_snapshot_is_detected(self):
        path = self.text_file('a.txt', 'safe evidence')
        source = self.corpus.ingest(path)
        block = self.corpus.search('safe', [source])[0]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot = self.workspace / 'sources' / digest / 'original.txt'
        snapshot.chmod(0o600)
        snapshot.write_text('tampered')
        for action in (lambda: self.corpus.ingest(path),
                       lambda: self.corpus.get_block(block['id'], [source]),
                       lambda: self.corpus.search('safe', [source])):
            with self.assertRaises(ValueError):
                action()

    def test_limits_bound_complete_search_packet_and_keep_full_block(self):
        path = self.root / 'huge.pdf'
        make_pdf(path, ['Background detail. ' * 1500+'\n\nevidence holds.', 'evidence two', 'evidence three'])
        source = self.corpus.ingest(path)
        results = self.corpus.search('evidence', [source], limit=1, max_tokens=220)
        self.assertEqual(len(results), 1)
        self.assertLessEqual(estimate_tokens(results), 220)
        self.assertTrue(results[0]['truncated'])
        self.assertGreater(len(self.corpus.get_block(results[0]['id'], [source])['text']), len(results[0]['text']))
        self.assertEqual(self.corpus.search('evidence', [source], limit=0), [])
        self.assertEqual(self.corpus.search('evidence', [source], max_tokens=1), [])
        self.assertEqual(self.corpus.search('', [source]), [])

    def test_other_database_tables_are_untouched(self):
        with closing(sqlite3.connect(self.workspace / 'state.sqlite')) as db:
            with db:
                db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY)')
                db.execute("INSERT INTO tasks VALUES ('keep')")
        self.corpus.ingest(self.text_file('a.txt', 'evidence'))
        with closing(sqlite3.connect(self.workspace / 'state.sqlite')) as db:
            self.assertEqual(db.execute('SELECT id FROM tasks').fetchall(), [('keep',)])

    def test_truncated_snippet_retains_matching_text_at_end_of_page(self):
        path = self.root / 'tail.pdf'
        make_pdf(path, ['Background detail. ' * 1500 + '\n\nrarekeyword evidence.'])
        source = self.corpus.ingest(path)
        results = self.corpus.search('rarekeyword', [source], max_tokens=220)
        self.assertEqual(len(results), 1)
        self.assertIn('rarekeyword', results[0]['text'])
        self.assertTrue(results[0]['truncated'])
        self.assertLessEqual(estimate_tokens(results), 220)

    def test_unsupported_type_rejected(self):
        path = self.text_file('a.html', '<p>evidence</p>')
        with self.assertRaises(ValueError):
            self.corpus.ingest(path)
        self.assertEqual(self.corpus.list_sources(), [])


if __name__ == '__main__':
    unittest.main()
