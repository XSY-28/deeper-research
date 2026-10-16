"""Immutable local source snapshots and scoped, deterministic keyword retrieval."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3

import pypdf
from pypdf import PdfReader

from .contracts import estimate_tokens


_TEXT_VERSION = 'text-lines-v1'
_PDF_VERSION = 'pypdf-physical-pages-v1'
_CJK = r'[\u3400-\u4dbf\u4e00-\u9fff]'

# General query vocabulary, not paper-specific answers or page anchors.
_QUERY_CONCEPTS = (
    ('停止|终止|退出|上限', 'stop stopping terminate termination limit maximum max finish'),
    ('迭代|重试|回合|试次', 'iteration iterative trial retry repeated'),
    ('反馈|评估|评价|评审', 'feedback evaluator evaluation critic assessment'),
    ('历史|记忆|保留', 'history memory retain retained buffer'),
    ('内存|上下文|窗口', 'memory context window'),
    ('参数|权重', 'parameter weight'),
    ('训练', 'train training finetuning'),
    ('梯度', 'gradient'),
    ('更新', 'update updating change'),
    ('修改|修订', 'refine refinement revision'),
    ('单调|严格增加', 'monotonic monotonically increase iteration'),
    ('失败|失效|风险|错误', 'failure fail error incorrect false'),
    ('具体|可执行|精细', 'specific actionable concrete'),
    ('组合|结合|互斥', 'combine combination actor'),
    ('假设|限制|边界', 'assumption limitation constraint'),
    ('质量|维度|方面|权衡', 'quality aspect dimension tradeoff decline'),
    ('测试|假阳性|假阴性|误判', 'test suite false positive negative incorrect'),
    ('成本|调用|预算', 'cost call budget token'),
)
_QUERY_STOP = set('a an the and or of to in on for with from by is are be this that '
                  'how what which compare comparison paper papers version pdf'.split())


def _terms(text):
    text = text.casefold()
    words = re.findall(r'[a-z0-9_]+', text)
    terms = [w[:-1] if len(w)>4 and w.endswith('s') and not w.endswith(('ss','us','is')) else w
             for w in words]
    for run in re.findall(_CJK + '+', text):
        terms.extend(run)
        terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    return set(terms)


def _query_terms(query: str) -> set[str]:
    expansion = ' '.join(words for pattern, words in _QUERY_CONCEPTS if re.search(pattern, query))
    return (_terms(query + ' ' + expansion) - _QUERY_STOP)


def _source_regions(text: str, layout: str | None = None) -> list[tuple[int, int]]:
    """Contiguous source regions, never arbitrary byte windows.

    PDF layout contributes only whitespace boundaries, and only when its entire
    non-whitespace character stream equals the immutable indexed text. Thus
    column reordering cannot silently reassign a heading or change a quotation.
    """
    original=[i for i,c in enumerate(text) if not c.isspace()]
    shaped=layout if layout is not None else text
    positions=[i for i,c in enumerate(shaped) if not c.isspace()]
    if ''.join(text[i] for i in original)!=''.join(shaped[i] for i in positions):
        shaped=text;positions=original
    paragraphs=list(re.finditer(r'\S.*?(?=\n[ \t]*\n|\s*\Z)',shaped,re.S))
    if len(paragraphs)>1:
        import bisect
        regions=[];prefix=None
        for p in paragraphs:
            left=bisect.bisect_left(positions,p.start());right=bisect.bisect_left(positions,p.end())
            start,end=original[left],original[right-1]+1
            value=shaped[p.start():p.end()].strip()
            # A standalone short heading stays with its following paragraph.
            # No semantic class or document-specific label is inferred.
            heading='\n' not in value and len(value)<=100 and not re.search(r'[.!?。！？]$',value)
            if heading:
                if prefix is not None:regions.append((prefix[0],prefix[1]))
                prefix=(start,end);continue
            regions.append((prefix[0] if prefix else start,end));prefix=None
        if prefix:regions.append(prefix)
        return regions
    # An indexed TXT/Markdown block is already a complete paragraph. A PDF
    # without trustworthy recovered structure is no license to cut off an
    # earlier subject/condition. Oversized atomic regions stay unavailable.
    return [(0,len(text))] if text.strip() else []


class Corpus:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.database = self.workspace / 'state.sqlite'
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS corpus_sources (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    original_path TEXT NOT NULL, snapshot_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
                    created TEXT NOT NULL, url TEXT, status TEXT NOT NULL,
                    warnings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corpus_blocks (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    text TEXT NOT NULL, location TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES corpus_sources(id)
                );
                CREATE INDEX IF NOT EXISTS corpus_blocks_source
                    ON corpus_blocks(source_id);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _validate(self, source):
        snapshot = self.workspace / source['snapshot_path']
        try:
            actual = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"Snapshot unavailable: {source['id']}") from error
        if actual != source['sha256']:
            raise ValueError(f"Snapshot hash mismatch: {source['id']}")

    def ingest(self, path: Path, title: str | None = None, url: str | None = None) -> str:
        path = Path(path).resolve()
        extension = path.suffix.lower()
        if extension not in {'.txt', '.md', '.pdf'}:
            raise ValueError(f'Unsupported source type: {extension}')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        version = _PDF_VERSION + ':' + pypdf.__version__ if extension == '.pdf' else _TEXT_VERSION
        source_id = 'src-' + hashlib.sha256((digest + ':' + version).encode()).hexdigest()
        with self._connect() as db:
            existing = db.execute('SELECT * FROM corpus_sources WHERE id = ?', (source_id,)).fetchone()
        if existing:
            self._validate(existing)
            return source_id

        # Parse before publishing metadata or storing a new snapshot. Invalid
        # extraction cannot replace any previous source or its evidence blocks.
        if extension == '.pdf':
            blocks, warnings = self._parse_pdf(data)
        else:
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError as error:
                raise ValueError('Text sources must be UTF-8') from error
            blocks = self._parse_text(text)
            warnings = [] if blocks else ['Source contains no non-whitespace text.']

        relative = Path('sources') / digest / ('original' + extension)
        snapshot = self.workspace / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            with snapshot.open('xb') as output:
                output.write(data)
            snapshot.chmod(0o444)
        except FileExistsError:
            pass
        # Hash even an already-present snapshot: existence does not imply validity.
        self._validate({'id': source_id, 'snapshot_path': str(relative), 'sha256': digest})
        with self._connect() as db:
            db.execute('''INSERT OR IGNORE INTO corpus_sources
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                       (source_id, title or path.stem, str(path), str(relative), digest,
                        version, datetime.now(timezone.utc).isoformat(), url,
                        'warning' if warnings else 'ready', json.dumps(warnings)))
            for index, (text, location) in enumerate(blocks):
                block_id = source_id + ':b' + str(index + 1)
                db.execute('INSERT OR IGNORE INTO corpus_blocks VALUES (?, ?, ?, ?)',
                           (block_id, source_id, text, json.dumps(location)))
        return source_id

    @staticmethod
    def _parse_text(text):
        blocks = []
        lines = text.splitlines(keepends=True)
        start = None
        for index, line in enumerate(lines):
            if not line.strip():
                if start is not None:
                    blocks.append((''.join(lines[start:index]),
                                   {'line_start': start + 1, 'line_end': index}))
                    start = None
            elif start is None:
                start = index
        if start is not None:
            blocks.append((''.join(lines[start:]),
                           {'line_start': start + 1, 'line_end': len(lines)}))
        return blocks

    @staticmethod
    def _parse_pdf(data):
        blocks = []
        warnings = ['PDF text extraction cannot validate complex equations or tables; '
                    'inspect the original page. No OCR is performed.']
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ValueError('Encrypted PDF sources are unsupported')
            for number, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ''
                except Exception as error:
                    warnings.append(f'Page {number}: text extraction failed ({type(error).__name__}).')
                    continue
                if text.strip():
                    blocks.append((text, {'page': number, 'bbox': None}))
                else:
                    warnings.append(f'Page {number}: empty or scanned; no text extracted.')
        except Exception as error:
            raise ValueError('PDF extraction failed') from error
        return blocks, warnings

    def list_sources(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute('SELECT * FROM corpus_sources ORDER BY created, id').fetchall()
        results = []
        for row in rows:
            source = dict(row)
            source['warnings'] = json.loads(source['warnings'])
            try:
                self._validate(row)
            except ValueError as error:
                source['status'] = 'corrupt'
                source['warnings'].append(str(error))
            results.append(source)
        return results

    @staticmethod
    def _block(row):
        return {'id': row['id'], 'source_id': row['source_id'], 'text': row['text'],
                'location': json.loads(row['location']), 'title': row['title']}

    def get_block(self, block_id: str, source_ids: list[str]) -> dict:
        with self._connect() as db:
            row = db.execute('''SELECT b.*, s.title FROM corpus_blocks b
                JOIN corpus_sources s ON s.id = b.source_id WHERE b.id = ?''',
                             (block_id,)).fetchone()
            if row is None or row['source_id'] not in source_ids:
                raise KeyError('Block is not in the permitted source scope')
            source = db.execute('SELECT * FROM corpus_sources WHERE id = ?',
                                (row['source_id'],)).fetchone()
        self._validate(source)
        return self._block(row)

    def search(self, query: str, source_ids: list[str], limit: int = 8,
               max_tokens: int = 3000) -> list[dict]:
        terms = _query_terms(query)
        if not source_ids or not terms or limit <= 0 or max_tokens <= 0:
            return []
        scope = list(dict.fromkeys(source_ids))
        placeholders = ','.join('?' for _ in scope)
        with self._connect() as db:
            sources = db.execute(f'SELECT * FROM corpus_sources WHERE id IN ({placeholders})', scope).fetchall()
            rows = db.execute(f'''SELECT b.*, s.title FROM corpus_blocks b
                JOIN corpus_sources s ON s.id = b.source_id
                WHERE b.source_id IN ({placeholders}) ORDER BY b.id''', scope).fetchall()
        for source in sources:
            self._validate(source)
        content_terms = [_terms(row['text']) for row in rows]
        weights = {term: 1 + math.log((len(rows)+1) / (1+sum(term in ts for ts in content_terms)))
                   for term in terms}
        # Disambiguate loop termination from e.g. memory/context limits.
        # These are generic loop-bound phrases, never paper/page anchors.
        termination = bool(re.search(r'停止|终止|退出|\bstop\w*\b|\bterminat\w*\b', query, re.I))
        memory_topic = bool(re.search(r'内存|上下文|窗口|\bmemory\b|\bcontext\b', query, re.I))
        loop_topic = bool(re.search(r'试次|回合|迭代|重试|循环|\b(?:trials?|steps?|iterations?|attempts?|loop)\b', query, re.I))
        termination = termination and (not memory_topic or loop_topic)
        bound_pattern = r'\bmax(?:imum)?\s+(?:number\s+of\s+)?(?:\d+\s+)?(?:trials?|steps?|iterations?|attempts?)\b'
        def bound_bonus(text):
            return 8 if termination and re.search(bound_pattern, text, re.I) else 0
        scored = []
        for row, body_terms in zip(rows, content_terms):
            score = sum(weights[t] for t in terms & body_terms) + bound_bonus(row['text'])
            # A repeated title cannot outrank substantive content just because
            # the question names that paper. Use titles only as a fallback.
            if not score:
                score = .1 * len(terms & _terms(row['title']))
            if score:
                block = self._block(row)
                block['query_terms'] = sorted(t for t in terms if t.isascii())[:40]
                scored.append((score, block))
        scored.sort(key=lambda item: (-item[0], int(item[1]['id'].rsplit(':b',1)[-1])))
        grouped = {source_id: [] for source_id in source_ids}
        for item in scored:
            grouped[item[1]['source_id']].append(item)
        scored = []
        while any(grouped.values()):
            for items in grouped.values():
                if items:
                    scored.append(items.pop(0))
        block_budget = min(max_tokens, max(350, max_tokens//4))
        packet = []
        readers={}
        for _, block in scored:
            if len(packet) >= limit:
                break
            if estimate_tokens(packet + [block]) <= max_tokens and estimate_tokens([block]) <= block_budget:
                packet.append(block)
                continue
            layout=None
            source=next(s for s in sources if s['id']==block['source_id'])
            snapshot=self.workspace/source['snapshot_path']
            if snapshot.suffix.lower()=='.pdf' and block['location'].get('page'):
                try:
                    if block['source_id'] not in readers:readers[block['source_id']]=PdfReader(snapshot)
                    layout=readers[block['source_id']].pages[block['location']['page']-1].extract_text(extraction_mode='layout')
                except (pypdf.errors.PdfReadError,NotImplementedError):
                    # Optional structure recovery may fail on text accepted by
                    # default extraction. Keep only existing atomic paragraphs.
                    layout=None
            regions=_source_regions(block['text'],layout)
            best, best_score = None, 0
            for start,end in regions:
                snippet=dict(block,truncated=True,text_start=start,text_end=end,text=block['text'][start:end])
                if estimate_tokens(packet+[snippet])>max_tokens or estimate_tokens([snippet])>block_budget:
                    continue
                score = sum(weights[t] for t in terms & _terms(snippet['text'])) + bound_bonus(snippet['text'])
                if score > best_score:
                    best, best_score = snippet, score
            if best:
                packet.append(best)
        return packet
