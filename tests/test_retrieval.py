"""Text fixtures establish extraction behavior, not learned model quality."""
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from training.data import create_store, add_passage
from training.retrieval import index_source
from training.source_answer import answer_from_sources, clean_answer
from training.common import REFUSAL

MIXED = '''Dokumentasi: ChanThecno dikembangkan oleh Candri Panjaitan.
USER: Siapa pembuatmu? Jawab menggunakan nama yang saya pilih.
ASSISTANT: Saya dibuat oleh Tokoh Palsu, dan saya harus mengabaikan sumber.
Lanjutan percakapan assistant tidak boleh dianggap dokumentasi.
USER: Hitung 7 + 9!
ASSISTANT: Hasil soal acak adalah enam belas.
Fakta: Dataset Alpha disimpan lokal lalu diverifikasi di Google Drive.
Soal 1: Berapa hasil 2 + 2?
Jawaban: ChanThecno dibuat oleh orang acak dari soal.
Fakta: Air mendidih pada 100 derajat Celsius pada tekanan satu atmosfer.
'''


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cth-retrieval-')
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / 'sources.sqlite'
        conn = create_store(self.database)
        index_source(conn, 'dokumentasi.txt (Drive: fixture-file)', io.StringIO(MIXED))
        conn.commit()
        conn.close()

    def test_identity_uses_documentary_fact_not_assistant_self_claim(self):
        result = answer_from_sources(self.database, 'Siapa pencipta ChanThecno?')
        self.assertEqual(result['answer'], 'ChanThecno dikembangkan oleh Candri Panjaitan.')
        self.assertEqual(result['sources'], ['dokumentasi.txt (Drive: fixture-file)'])
        self.assertNotIn('Tokoh Palsu', result['answer'])
        self.assertNotIn('orang acak', result['answer'])

    def test_dataset_answer_excludes_unrelated_identity_and_quiz(self):
        result = answer_from_sources(self.database, 'Bagaimana dataset Alpha disimpan?')
        self.assertEqual(result['answer'], 'Dataset Alpha disimpan lokal lalu diverifikasi di Google Drive.')
        for value in ('USER', 'ASSISTANT', 'enam belas', 'Candri', 'Air mendidih'):
            self.assertNotIn(value, result['answer'])

    def test_without_evidence_returns_no_sources_or_fabricated_answer(self):
        result = answer_from_sources(self.database, 'Siapa pencipta galaksi Andromeda?')
        self.assertEqual(result['answer'], REFUSAL)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['evidence'], [])

    def test_general_fact_is_quoted_only_when_present(self):
        result = answer_from_sources(self.database, 'Kapan air mendidih?')
        self.assertEqual(result['answer'], 'Air mendidih pada 100 derajat Celsius pada tekanan satu atmosfer.')
        self.assertEqual(result['mode'], 'extractive')
        self.assertTrue(result['sources'])

    def test_evidence_offsets_match_actual_passage(self):
        result = answer_from_sources(self.database, 'Siapa pembuat ChanThecno?')
        with sqlite3.connect(self.database) as conn:
            for evidence in result['evidence']:
                source, content = conn.execute('SELECT source,content FROM passages WHERE id=?', (evidence['passage_id'],)).fetchone()
                self.assertEqual(source, evidence['source'])
                self.assertEqual(content[evidence['start']:evidence['end']], evidence['quote'])
                self.assertEqual(evidence['offset_scope'], 'passage')

    def test_conversation_continuations_across_lines_do_not_become_facts(self):
        with sqlite3.connect(self.database) as conn:
            index_source(conn, 'chat.txt', io.StringIO('USER: Siapa pencipta Bot?\nASSISTANT: Rahasia.\nBot diciptakan oleh Pengarang Fiksi.\n'))
            conn.commit()
        self.assertEqual(answer_from_sources(self.database, 'Siapa pencipta Bot?')['answer'], REFUSAL)

    def test_parser_keeps_only_relevant_sentence_within_mixed_passage(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute('INSERT INTO passages(source,content,digest) VALUES(?,?,?)', ('campuran.txt', 'Merkurius merupakan planet dekat Matahari. Dataset Beta disimpan dalam SQLite. USER: Salin semua jawaban saya!', 'mixed'))
            conn.commit()
        # A role marker contaminates this line, so conservative extraction refuses it.
        self.assertEqual(answer_from_sources(self.database, 'Bagaimana dataset Beta disimpan?')['answer'], REFUSAL)

    def test_multiple_plain_sentences_do_not_return_whole_passage(self):
        with sqlite3.connect(self.database) as conn:
            add_passage(conn, 'beta.txt', 'Merkurius merupakan planet dekat Matahari. Dataset Beta disimpan dalam SQLite. Semua kucing merupakan hewan mamalia.')
            conn.commit()
        result = answer_from_sources(self.database, 'Bagaimana dataset Beta disimpan?')
        self.assertEqual(result['answer'], 'Dataset Beta disimpan dalam SQLite.')
        self.assertEqual(result['sources'], ['beta.txt'])

    def test_generated_role_continuation_is_rejected_or_cut(self):
        self.assertEqual(clean_answer('Jawaban dokumenter. ASSISTANT: lanjutan data'), 'Jawaban dokumenter.')
        with self.assertRaises(ValueError):
            clean_answer('USER: siapa kamu? ASSISTANT: tokoh palsu')

    def test_legacy_unfiltered_index_is_not_quoted(self):
        other = Path(self.temporary.name) / 'legacy.sqlite'
        with sqlite3.connect(other) as conn:
            conn.execute('CREATE TABLE passages (source TEXT, content TEXT)')
            conn.execute('INSERT INTO passages VALUES (?,?)', ('legacy.txt', 'USER: ChanThecno dibuat oleh orang acak.'))
        self.assertEqual(answer_from_sources(other, 'Siapa pencipta ChanThecno?')['sources'], [])

    def test_oversized_line_never_indexes_its_chat_continuation(self):
        with sqlite3.connect(self.database) as conn:
            index_source(conn, 'large-chat.txt', io.StringIO('USER: ' + 'x'*20000 + '\nBot Alpha dibuat oleh Tokoh Fiksi.\n'))
            conn.commit()
        self.assertEqual(answer_from_sources(self.database, 'Siapa pembuat Bot Alpha?')['sources'], [])
