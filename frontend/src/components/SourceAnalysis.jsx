const FORMATS = { plain_text: "Teks biasa", conversation_qa: "Percakapan USER/ASSISTANT", mixed: "Campuran fakta dan percakapan", structured_jsonl_qa: "QA JSONL terstruktur" };
export default function SourceAnalysis({ report }) {
  if (!report) return <p className="cth-muted">Analisis sumber belum tersedia. Jangan menganggap kutipan sudah siap.</p>;
  return <div className="cth-source-analysis">
    <p><strong>{report.training_samples} unit latihan sebelum tokenizer</strong> · {report.qa_pairs} pasangan QA terdeteksi · <strong>{report.searchable_quotes} kutipan dapat dicari</strong> · {report.skipped_lines} baris dengan bagian dilewati untuk indeks</p>
    <p className="cth-muted">Unit awal adalah blok teks 16.000 karakter untuk TXT atau record QA untuk JSONL, bukan jumlah sequence/token training. Jumlah sampel train/evaluasi yang sebenarnya baru tersedia setelah persiapan tokenizer. Deteksi QA tidak berarti jawaban ASSISTANT dipercaya sebagai fakta.</p>
    {!report.sources_ready && <p role="alert" className="cth-muted">{report.warning || "Indeks sumber kosong. Setujui QA terkurasi yang telah diperiksa atau pisahkan berkas fakta proyek; tidak ada kutipan yang siap digunakan."}</p>}
    {report.files && <details><summary>Format dan bagian yang dilewati</summary>{report.files.map((item, index) => <div key={item.dataset_id || index}>
      <strong>{item.name || item.file_id}</strong> · {FORMATS[item.format] || item.format} · peran {item.source_mode || "filtered"} · {item.searchable_quotes} kutipan
      {item.skipped?.map((entry, position) => <p className="cth-muted" key={position}>Baris {entry.line}: {entry.reason}</p>)}
      {item.skipped_lines > (item.skipped?.length || 0) && <small>Rincian dibatasi; total {item.skipped_lines} baris dilewati.</small>}
    </div>)}</details>}
  </div>;
}
