# Architecture

This document describes how the plugin is structured, how the index is laid out, and where to
extend it.

## Overview

```
Zotero (Gecko)                          Local consumer (agent / script)
┌────────────────────────────┐          ┌──────────────────────────────┐
│ PDFWorker.getFullText()    │          │ curl / HTTP client           │
│  ↓ pages (\f separated)    │          │  127.0.0.1:23119/retrieval/* │
│ chunker (≈520 chars)       │          └──────────────┬───────────────┘
│  ↓ chunks + page numbers   │                         │
│ BM25 (Intl.Segmenter)      │◄────────────────────────┘
│ Ollama bge-m3 embeddings   │  (optional, local)
│ RRF fusion / optional rank │
└──────────┬─────────────────┘
           │ writes
           ▼
  <profile>/retrieval-index/
    chunks.json      vectors.b64      meta.json
    ocr/<itemKey>.json                tables/<itemKey>.json
```

## Components

| File | Responsibility |
|---|---|
| `bootstrap.js` | Zotero bootstrap entry point: starts/stops the plugin, registers the HTTP endpoints and menu items. |
| `content/retrieval.js` | Index construction (`build`), incremental update, search pipeline, endpoint handlers, tuning. |
| `content/core.js` | Pure helpers that are unit-testable outside Zotero: tokenizer, chunker, BM25, vector pack/unpack. |
| `content/protocol-text.js` | Generated from `docs/agent-usage-protocol.md`; served by `/retrieval/protocol`. |
| `build.py` | Packages the XPI from the manifest and source files. |

## Build pipeline

1. Enumerate PDF attachments in all accessible libraries (user + group).
2. For each attachment:
   - If an OCR sidecar exists (`ocr/<itemKey>.json`), use it page by page.
   - Otherwise call `Zotero.PDFWorker.getFullText(itemID)`, which returns text separated by
     `\f` per page.
   - Fall back to Zotero's full-text cache (`.zotero-ft-cache`) when the PDF is missing.
   - Split pages into ~520-character chunks that do not cross page boundaries.
   - Optionally embed `title + "\n" + chunk` with `bge-m3` (batch via local Ollama).
3. Deduplicate identical chunk texts across libraries (prefer the configured library priority).
4. Persist `chunks.json`, `vectors.b64` (int8-quantized), and `meta.json`.

Incremental mode (`/retrieval/build?incremental=1`) reuses chunks and vectors for items whose
Zotero `version` is unchanged, and reprocesses everything else.

## Index format

`chunks.json` — array of objects:

```json
{
  "cid": "…", "itemKey": "ABCD1234", "parentKey": "EFGH5678",
  "libraryID": 3, "title": "…", "page": 12, "pageSource": "pdf",
  "role": "front|body|table", "orphan": false, "ocr": false,
  "text": "…"
}
```

`vectors.b64` — base64 of `Int8Array(chunks.length × dim)`, each component `round(x * 127)` where
`x` is the L2-normalized embedding. `meta.json` records `dim`, `model`, `count`, per-item
versions, and build statistics.

`ocr/<itemKey>.json` — `{ "pages": ["page text", …], "ocrPages": [1, 3, …] }`.
`tables/<itemKey>.json` — `{ "tables": [{ "page": 3, "caption": "…", "markdown": "| … |" }] }`.

## Search pipeline

1. Tokenize the query with `Intl.Segmenter` (ICU) for BM25.
2. If a dense model is available, embed the query and compute cosine similarity against the index
   vectors; otherwise run BM25 only.
3. Fuse `bm25` and `dense` rankings with Reciprocal Rank Fusion (`rrfK`, `wBm25`, `wDense`).
4. Cap results per paper (`capPerPaper`) and deduplicate identical chunk texts.
5. Optionally rerank the fused candidates with the local cross-encoder service (`rerank*`).
6. Append table supplements (`tableSupplement*`) from the recalled papers — these never affect
   ranking.
7. Return `{evidence, results}`: `evidence` contains counts and source hints, `results` contains
   the passages with page anchors.

## HTTP layer

Endpoints are registered on `Zotero.Server.Endpoints['/retrieval/*']` and served by Zotero's
local server. Requests are read-only except:

- `/retrieval/build` — starts an index build;
- `/retrieval/admin/tune` — writes tuning preferences.

## Extension points

- Add a new retrieval branch: extend `content/retrieval.js` around the fusion step and expose a
  weight in `TUNE_SPEC`.
- Add a new sidecar type: follow the `ocr/` / `tables/` pattern and read it during `build`.
- Add a new endpoint: register it in `registerEndpoints()` and document it in the README.

## Design constraints

- The code runs inside Zotero's Gecko runtime (no bundler, no Node APIs, no `fetch` to external
  hosts).
- Keep dependencies out of the XPI. Anything heavy (PDF parsing, OCR, reranking) belongs in
  optional `tools/` services that run as separate processes.
- Backward compatibility of the index format is handled through `meta.indexSchema` migration in
  `loadIndex()`.
