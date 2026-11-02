"""Conservative, bounded extraction of documentary evidence, with explicitly approved QA answer extraction."""
import hashlib
import re
import sqlite3

STOPWORDS = set('apa apakah itu yang di ke dari dan atau untuk dengan saya aku kamu tolong jelaskan siapa bagaimana berapa kapan dimana adalah tentang sebutkan manfaat informasi proyek utama'.split())
ROLE = re.compile(r'(?:\b(?:user|assistant|system|human|pengguna|asisten)\s*:|<\|(?:im_start|im_end|endoftext)\|>)', re.I)
HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:fakta|sumber|dokumentasi|catatan proyek)\s*:\s*', re.I)
EXERCISE = re.compile(r'^\s*(?:(?:soal|pertanyaan|jawaban|question|answer|latihan)\b|[A-D][.)]\s|\d+[.)]\s*(?:hitung|berapa|siapa|pilih))', re.I)
DIRECTIVE = re.compile(r'^\s*(?:abaikan|ikuti|jawablah|jawab|instruksi|anggap|ignore|you are|saya|aku|kamu|anda)\b', re.I)
IDENTITY = re.compile(r'\b(?:pencipta|pembuat|pendiri|dikembangkan|dibuat|didirikan|diciptakan)\b', re.I)


def words(text):
    return set(re.findall(r'[^\W_]+', text.lower(), flags=re.UNICODE)) - STOPWORDS


def documentary_spans(content, state=None):
    state = state if state is not None else {'blocked': False}
    offset = 0
    for line in content.splitlines(keepends=True):
        heading = HEADING.match(line)
        if heading:
            state['blocked'] = False
        if ROLE.search(line) or EXERCISE.match(line):
            state['blocked'] = True
            offset += len(line)
            continue
        if state['blocked']:
            offset += len(line)
            continue
        start = heading.end() if heading else 0
        for match in re.finditer(r'[^.!?\n]+(?:[.!?](?=\s|$)|$)', line[start:]):
            raw = match.group()
            quote = raw.strip().lstrip('-* ').strip()
            if (not quote or quote.endswith('?') or len(quote) < 15 or len(quote) > 800
                    or DIRECTIVE.match(quote) or ROLE.search(quote)
                    or len(words(quote)) < 2):
                continue
            position = offset + start + match.start() + raw.index(quote)
            yield quote, position, position + len(quote)
        offset += len(line)


def create_index(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS passages (id INTEGER PRIMARY KEY, source TEXT, content TEXT, digest TEXT UNIQUE)')
    if 'qa_question' not in {row[1] for row in conn.execute('PRAGMA table_info(passages)')}:
        conn.execute("ALTER TABLE passages ADD COLUMN qa_question TEXT NOT NULL DEFAULT ''")
    conn.execute('CREATE TABLE IF NOT EXISTS retrieval_meta (version INTEGER NOT NULL)')
    if conn.execute('SELECT COUNT(*) FROM retrieval_meta').fetchone()[0] == 0:
        conn.execute('INSERT INTO retrieval_meta VALUES (2)')


def store_quote(conn, source, quote, question=''):
    digest = hashlib.sha256((source + '\0' + question + '\0' + quote).encode()).hexdigest()
    return conn.execute('INSERT OR IGNORE INTO passages(source,content,digest,qa_question) VALUES(?,?,?,?)', (source, quote, digest, question)).rowcount


SOURCE_MODES = {'filtered', 'training_only', 'facts_only', 'curated_qa'}
ANALYSIS_VERSION = 2
TURN = re.compile(r'^\s*(USER|ASSISTANT|SYSTEM|HUMAN|PENGGUNA|ASISTEN)\s*:\s*(.*)', re.I)


# Approval is a provenance choice, never permission to execute dataset instructions.
QA_INSTRUCTION = re.compile(
    r'\b(?:abaikan|ignore|disregard|instruksi|instructions?|system\s+prompt|'
    r'ikuti|jawablah|anggap|berpura|pretend|you\s+are|act\s+as|'
    r'bocorkan|rahasia|token|password|jalankan|execute|sudo|jawab|override|reveal|perintah|prompt|hapus|ubah|kirim|send)\b', re.I)


def curated_answers(handle):
    """Bounded USER→ASSISTANT pairs. Only answers, never roles/questions, are quoted."""
    role, question, answer, answer_lines, invalid = None, '', '', [], False
    for number, line in enumerate(iter(lambda: handle.readline(8193), ''), 1):
        if number == 1:
            line = line.lstrip('\ufeff')
        if len(line) > 8192:
            invalid = True
            while line and not line.endswith('\n'):
                line = handle.readline(8193)
            continue
        turn = TURN.match(line)
        if turn:
            if role == 'answer' and question.strip() and answer.strip() and not invalid:
                yield question.strip(), answer, answer_lines
            name, content = turn.groups()
            if name.upper() in {'USER', 'HUMAN', 'PENGGUNA'}:
                role, question, answer, answer_lines, invalid = 'question', content, '', [], False
            elif name.upper() in {'ASSISTANT', 'ASISTEN'} and role == 'question':
                role, answer, answer_lines = 'answer', content + '\n', [number]
            else:
                role, question, answer, answer_lines, invalid = None, '', '', [], False
        elif role == 'question':
            if len(question) + len(line) <= 8192:
                question += '\n' + line
            else:
                invalid = True
        elif role == 'answer':
            if len(answer) + len(line) <= 8192:
                answer += line
                answer_lines.append(number)
            else:
                invalid = True
    if role == 'answer' and question.strip() and answer.strip() and not invalid:
        yield question.strip(), answer, answer_lines


def index_source(conn, source, handle, source_mode='filtered'):
    if source_mode not in SOURCE_MODES:
        raise ValueError('Peran sumber tidak didukung.')
    report = {'analysis_version': ANALYSIS_VERSION, 'source_mode': source_mode, 'format': 'plain_text',
              'qa_pairs': 0, 'training_samples': 0, 'sample_count_kind': 'text_blocks_before_tokenizer',
              'searchable_quotes': 0, 'skipped_lines': 0, 'skipped': [], 'lines': 0}
    state = {'blocked': False}
    role_lines = plain_lines = characters = 0
    question = False
    reading_question = False
    waiting_answer = False
    while True:
        line = handle.readline(8193)
        if not line:
            break
        report['lines'] += 1
        characters += len(line)
        if report['lines'] == 1:
            line = line.lstrip('\ufeff')
        if '\x00' in line:
            raise ValueError('Sumber teks mengandung NUL biner.')
        reason = None
        if len(line) > 8192:
            if TURN.match(line):
                role_lines += 1
            reason = 'baris terlalu panjang untuk kutipan'
            state['blocked'] = True
            while line and not line.endswith('\n'):
                line = handle.readline(8193)
                characters += len(line)
        else:
            turn = TURN.match(line)
            if turn:
                role_lines += 1
                role, text = turn.groups()
                if role.upper() in {'USER', 'HUMAN', 'PENGGUNA'}:
                    question = bool(text.strip())
                    reading_question = True
                    waiting_answer = False
                elif role.upper() in {'ASSISTANT', 'ASISTEN'}:
                    reading_question = False
                    waiting_answer = question
                    if waiting_answer and text.strip():
                        report['qa_pairs'] += 1
                        waiting_answer = question = False
                else:
                    question = reading_question = waiting_answer = False
            elif reading_question and line.strip():
                question = True
            elif waiting_answer and line.strip():
                report['qa_pairs'] += 1
                question = waiting_answer = False
            quotes = list(documentary_spans(line, state))
            if not state['blocked'] and line.strip() and not ROLE.search(line):
                plain_lines += 1
            if source_mode in {'training_only', 'curated_qa'}:
                reason = ('bukan kutipan QA terkurasi yang lolos filter (role/pertanyaan/instruksi/duplikat)' if source_mode == 'curated_qa'
                          else 'berkas khusus latihan; tidak menjadi sumber')
            elif quotes:
                added = sum(store_quote(conn, source, quote) for quote, _, _ in quotes)
                report['searchable_quotes'] += added
                if added < len(quotes):
                    reason = 'kutipan duplikat; teks asli tetap disimpan'
            elif line.strip():
                reason = 'percakapan/soal/instruksi atau teks tidak memenuhi filter kutipan'
        if reason:
            report['skipped_lines'] += 1
            if len(report['skipped']) < 25:
                report['skipped'].append({'line': report['lines'], 'reason': reason})
    if source_mode == 'curated_qa':
        handle.seek(0)
        accepted_lines = set()
        accepted_count = 0
        detailed_lines = {item['line'] for item in report['skipped']}
        for prompt, answer, line_numbers in curated_answers(handle):
            if QA_INSTRUCTION.search(prompt + '\n' + answer) or ROLE.search(prompt + answer):
                continue
            quotes = list(documentary_spans(answer))
            added = sum(store_quote(conn, source, quote, prompt if len(quotes) == 1 else '') for quote, _, _ in quotes)
            report['searchable_quotes'] += added
            if added:
                for number, line in zip(line_numbers, answer.splitlines()):
                    if any(quote in line for quote, _, _ in quotes):
                        accepted_count += 1
                        if number in detailed_lines:
                            accepted_lines.add(number)
        report['skipped_lines'] = max(0, report['skipped_lines'] - accepted_count)
        report['skipped'] = [item for item in report['skipped'] if item['line'] not in accepted_lines]
    report['sources_ready'] = report['searchable_quotes'] > 0
    report['format'] = ('mixed' if plain_lines else 'conversation_qa') if role_lines else 'plain_text'
    report['training_samples'] = 0 if source_mode == 'facts_only' else (characters + 15999) // 16000
    return report


def source_summary(reports, details=False):
    quotes = sum(item['searchable_quotes'] for item in reports)
    files = reports if details else [{key: value for key, value in item.items() if key != 'skipped'} for item in reports]
    return {'files': files, 'searchable_quotes': quotes,
            'training_samples': sum(item['training_samples'] for item in reports),
            'sample_count_kind': 'text_blocks_before_tokenizer',
            'qa_pairs': sum(item['qa_pairs'] for item in reports),
            'skipped_lines': sum(item['skipped_lines'] for item in reports),
            'sources_ready': quotes > 0,
            'warning': None if quotes else 'Indeks sumber kosong. Tidak ada kutipan yang dapat dicari; percakapan latihan tidak otomatis menjadi fakta. Setujui QA terkurasi yang telah diperiksa atau pisahkan berkas fakta proyek.'}


def retrieve(database, question, limit=3):
    keys = words(question)
    if not keys:
        return []
    identity_question = bool(re.search(r'\b(?:pencipta|pembuat|pendiri|dikembangkan|dibuat|diciptakan)\b', question, re.I))
    if identity_question:
        keys -= {'pencipta', 'pembuat', 'pendiri', 'dikembangkan', 'dibuat', 'diciptakan', 'oleh'}
    if not keys:
        return []
    matches = []
    with sqlite3.connect(database) as conn:
        try:
            version = conn.execute('SELECT version FROM retrieval_meta').fetchone()
        except sqlite3.OperationalError:
            return []
        if version != (2,):
            return []
        columns = {row[1] for row in conn.execute('PRAGMA table_info(passages)')}
        question_column = 'qa_question' if 'qa_question' in columns else "''"
        for passage_id, source, content, prompt in conn.execute(f'SELECT id,source,content,{question_column} FROM passages'):
            for quote, start, end in documentary_spans(content):
                tokens = words(quote)
                overlap = keys & (tokens | words(prompt))
                coverage = len(overlap) / len(keys)
                if coverage < 1:
                    continue
                if identity_question and not IDENTITY.search(quote + ' ' + prompt):
                    continue
                # Prefer concise sentences covering the question rather than large keyword dumps.
                score = coverage + len(overlap) / max(len(tokens), 1)
                record = {'source': source, 'quote': quote, 'passage_id': passage_id,
                          'start': start, 'end': end, 'offset_scope': 'passage'}
                matches.append((score, record))
                matches.sort(key=lambda item: (-item[0], len(item[1]['quote']), item[1]['source']))
                del matches[limit:]
    unique = []
    seen = set()
    for _, record in matches:
        if record['quote'] not in seen:
            seen.add(record['quote'])
            unique.append(record)
    return unique
