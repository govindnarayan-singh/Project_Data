# Marathi Government GR Retrieval - Open Source Starter

This starter implements the first production foundation:

PDF -> digital text or Marathi OCR -> normalization -> metadata -> chunks -> BGE-M3 embeddings -> PostgreSQL + pgvector -> semantic search

It intentionally does **not** add agents yet. Retrieval quality must be tested first.

## Expected folder layout

Place this starter's files in the root of your existing project, so the structure becomes roughly:

```text
LLM_Project_data/
├── .cache_local/
├── GRs/
│   ├── Finance Department/
│   │   ├── *.pdf
│   ├── Housing Department/
│   │   ├── *.pdf
│   └── ...
├── src/
├── sql/
├── logs/
├── compose.yaml
├── requirements.txt
└── .env
```

If you do not want to merge folders yet, you can also keep this starter in a separate directory and point `GR_ROOT` in `.env` to the actual GR folder.

## 1. Python environment

Recommended: Python 3.11 or 3.12.

```bash
python -m venv .venv
```

Linux/WSL:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Then:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2. Install Tesseract Marathi OCR

Ubuntu/WSL example:

```bash
sudo apt update
sudo apt install -y tesseract-ocr tesseract-ocr-mar tesseract-ocr-eng
```

Verify:

```bash
tesseract --list-langs
```

You should see `mar` and `eng`.

## 3. Start PostgreSQL + pgvector

Copy configuration first:

```bash
cp .env.example .env
```

With Podman + a Compose provider:

```bash
podman compose up -d
```

The database listens on `localhost:5432` with the credentials in `.env.example`.

## 4. Test only 3 PDFs first

Never start by processing all 2715 documents.

```bash
python -m src.ingest --limit 3
```

If one of those PDFs is scanned, Tesseract will be used only for pages where direct text extraction looks poor.

## 5. Audit extraction quality

```bash
python -m src.audit --limit 20
```

Check:

- how many pages used OCR
- which pages have very poor text
- how often GR number/date/subject were missed

Open `logs/ingest.log` for failures.

## 6. Search in Marathi

```bash
python -m src.search "शासकीय कर्मचाऱ्यांच्या निवृत्तीवेतनाबाबत शासन निर्णय" --top-k 8
```

Filter to one department:

```bash
python -m src.search "अनुदान मंजुरी" --department "Finance Department" --top-k 8
```

You can also test an English query against the Marathi corpus because BGE-M3 is multilingual:

```bash
python -m src.search "government decision about pension benefits" --top-k 8
```

## 7. Scale gradually

Suggested sequence:

```bash
python -m src.ingest --limit 20
python -m src.audit
```

Then one department:

```bash
python -m src.ingest --department "Finance Department"
```

Then all PDFs:

```bash
python -m src.ingest
```

Ingestion is idempotent by SHA-256: unchanged files are skipped. Use `--force` only when you intentionally want to rebuild existing files.

## 8. Retrieval evaluation

Copy:

```text
data/eval_queries.example.csv
```

to something like:

```text
data/eval_queries.csv
```

Add 30-100 real user questions for which you know the correct GR file.

Run:

```bash
python -m src.evaluate data/eval_queries.csv --k 5
```

Do this before building RAG or agents.

## Why PostgreSQL + pgvector instead of a separate vector database?

For only a few thousand GRs and tens of thousands of chunks, one PostgreSQL database is simpler. Metadata and vectors can be joined transactionally, and pgvector provides cosine nearest-neighbour search and HNSW indexing. You can split the vector store later if scale or operational requirements justify it.

## Next phase

After retrieval quality is acceptable, add:

1. richer metadata extraction
2. GR-to-GR relationship extraction: REFERENCES, AMENDS, SUPERSEDES, CANCELS, EXTENDS
3. hybrid lexical + semantic retrieval and reranking
4. RAG response generation with page-level citations
5. drafting workflow / agents only after the above are reliable
