from .retrieval import create_index, documentary_spans, index_source, store_quote, source_summary, SOURCE_MODES
import hashlib
import json
import sqlite3
from pathlib import Path

from .common import REFUSAL, SYSTEM, file_hash


def dataset_manifest(directory):
    directory = Path(directory).resolve()
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink tidak diperbolehkan pada direktori dataset.")
        if path.is_file() and path.suffix.lower() in {".txt", ".jsonl"}:
            if directory not in path.resolve().parents:
                raise ValueError("Berkas berada di luar direktori dataset.")
            if path.stat().st_size == 0:
                raise ValueError(f"Berkas kosong: {path.name}")
            files.append({"name": str(path.relative_to(directory)), "sha256": file_hash(path), "size": path.stat().st_size})
    if not files:
        raise ValueError("Tidak ada TXT atau JSONL dalam direktori dataset.")
    return files


def validate_qa(record):
    if not isinstance(record, dict):
        raise ValueError("Setiap baris JSONL harus berupa objek.")
    for key in ("context", "question", "answer"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"Kolom {key} harus berisi teks.")
        if "\x00" in record[key]:
            raise ValueError("Data mengandung karakter biner NUL.")
    answer = record["answer"].strip()
    context = record["context"].strip()
    if answer != REFUSAL and answer not in context:
        raise ValueError("Jawaban JSONL harus kutipan persis dari context, atau kalimat penolakan resmi. Jangan mengarang pasangan QA.")
    return {key: record[key].strip() for key in ("context", "question", "answer")}


def encode_qa(tokenizer, record, max_length):
    record = validate_qa(record)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"SUMBER:\n{record['context']}\n\nPERTANYAAN:\n{record['question']}"},
    ]
    prompt_ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    answer_ids = tokenizer.encode(record["answer"], add_special_tokens=False) + [tokenizer.eos_token_id]
    input_ids = prompt_ids + answer_ids
    if len(input_ids) > max_length:
        raise ValueError("Contoh QA melebihi max-length. Pecah context atau naikkan max-length; jawaban tidak dipotong diam-diam.")
    labels = [-100] * len(prompt_ids) + answer_ids
    return input_ids, labels, record["context"]


def create_store(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, digest TEXT UNIQUE, group_id TEXT, input_ids TEXT, labels TEXT, split TEXT)")
    create_index(conn)
    return conn


def add_passage(conn, source, content):
    for quote, _, _ in documentary_spans(content):
        store_quote(conn, source, quote)



def add_sample(conn, input_ids, labels, group):
    encoded = json.dumps(input_ids, separators=(",", ":"))
    digest = hashlib.sha256((encoded + json.dumps(labels)).encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO samples(digest,group_id,input_ids,labels) VALUES(?,?,?,?)", (digest, group, encoded, json.dumps(labels)))


def prepare_data(directory, manifest, tokenizer, max_length, database, seed, source_names=None, source_modes=None):
    conn = create_store(database)
    modes = set()
    reports = []
    training_excluded = []
    try:
        for item in manifest:
            path = Path(directory) / item["name"]
            source = (source_names or {}).get(item["name"], item["name"])
            source_mode = (source_modes or {}).get(item['name'], 'filtered')
            if source_mode not in SOURCE_MODES:
                raise ValueError('Peran sumber tidak didukung.')
            if source_mode == 'facts_only':
                training_excluded.append(item['name'])
            if path.suffix.lower() == ".jsonl":
                modes.add("supervised_qa")
                records = 0
                before_quotes = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
                with path.open(encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                            record = validate_qa(record)
                            context = record['context']
                            if source_mode != 'facts_only':
                                ids, labels, context = encode_qa(tokenizer, record, max_length)
                        except (ValueError, TypeError) as error:
                            raise ValueError(f"{item['name']} baris {number}: {error}") from None
                        records += 1
                        if source_mode != 'facts_only':
                            group = hashlib.sha256(context.encode()).hexdigest()
                            add_sample(conn, ids, labels, group)
                        if source_mode != 'training_only':
                            add_passage(conn, source, context)
                reports.append({'name': item['name'], 'format': 'structured_jsonl_qa', 'source_mode': source_mode,
                    'training_samples': 0 if source_mode == 'facts_only' else records,
                    'sample_count_kind': 'qa_records_before_tokenizer', 'qa_pairs': records,
                    'searchable_quotes': conn.execute('SELECT COUNT(*) FROM passages').fetchone()[0] - before_quotes,
                    'skipped_lines': records if source_mode == 'training_only' else 0})
            else:
                modes.add("text_adaptation")
                if source_mode != "facts_only":
                    with path.open(encoding="utf-8", errors="strict") as handle:
                        while True:
                            text = handle.read(16000)
                            if not text:
                                break
                            if "\x00" in text:
                                raise ValueError(f"Data biner ditemukan: {item['name']}")
                            if not text.strip():
                                continue
                            tokens = tokenizer.encode(text, add_special_tokens=False)
                            for start in range(0, len(tokens), max_length - 1):
                                ids = tokens[start:start + max_length - 1] + [tokenizer.eos_token_id]
                                if len(ids) >= 2:
                                    group = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
                                    add_sample(conn, ids, ids.copy(), group)
            if path.suffix.lower() != ".jsonl":
                with path.open(encoding="utf-8", errors="strict") as handle:
                    report = index_source(conn, source, handle, source_mode)
                    report.update(name=item['name'])
                    reports.append(report)
            conn.commit()
        groups = [row[0] for row in conn.execute("SELECT DISTINCT group_id FROM samples")]
        if len(groups) < 10:
            raise ValueError("Butuh minimal 10 kelompok teks/context berbeda untuk train dan validasi. Contoh kecil hanya untuk uji kode, bukan jaminan kualitas.")
        groups.sort(key=lambda group: hashlib.sha256(f"{seed}:{group}".encode()).hexdigest())
        eval_groups = set(groups[:max(1, len(groups) // 10)])
        for group in groups:
            conn.execute("UPDATE samples SET split=? WHERE group_id=?", ("eval" if group in eval_groups else "train", group))
        conn.commit()
        counts = dict(conn.execute("SELECT split,COUNT(*) FROM samples GROUP BY split"))
        passages = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        analysis = source_summary(reports)
        analysis.update(searchable_quotes=passages, sources_ready=passages > 0)
        if passages:
            analysis['warning'] = None
        return {"counts": counts, "modes": sorted(modes), "passages": passages,
                "source_analysis": analysis, "training_excluded": training_excluded}
    finally:
        conn.close()


class StoredDataset:
    def __init__(self, database, split):
        self.database = str(database)
        with sqlite3.connect(self.database) as conn:
            self.ids = [row[0] for row in conn.execute("SELECT id FROM samples WHERE split=? ORDER BY id", (split,))]
        self.conn = None

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, index):
        if self.conn is None:
            self.conn = sqlite3.connect(self.database)
        ids, labels = self.conn.execute("SELECT input_ids,labels FROM samples WHERE id=?", (self.ids[index],)).fetchone()
        ids = json.loads(ids)
        return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": json.loads(labels)}


def collate_examples(examples, pad_token_id):
    import torch
    length = max(len(row["input_ids"]) for row in examples)
    return {
        "input_ids": torch.tensor([row["input_ids"] + [pad_token_id] * (length - len(row["input_ids"])) for row in examples]),
        "attention_mask": torch.tensor([row["attention_mask"] + [0] * (length - len(row["attention_mask"])) for row in examples]),
        "labels": torch.tensor([row["labels"] + [-100] * (length - len(row["labels"])) for row in examples]),
    }
