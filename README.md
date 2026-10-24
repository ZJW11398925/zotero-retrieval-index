# Retrieval Index — page-aware passage retrieval for Zotero

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Zotero](https://img.shields.io/badge/Zotero-7%2B-blue)](https://www.zotero.org/)
[![Release](https://img.shields.io/github/v/release/ZJW11398925/zotero-retrieval-index)](https://github.com/ZJW11398925/zotero-retrieval-index/releases)

A Zotero plugin that builds a **page-aware passage index** of your local library and exposes a
small local HTTP API for retrieval. It lets external tools — especially AI agents and RAG
pipelines — fetch *the few relevant passages* instead of stuffing whole papers into a context
window.

Everything runs locally: no cloud service, no telemetry, and no modification of your Zotero
library.

> 中文一句话：给 Zotero 本地文献库建**带真实页码的段落索引**，通过本机 HTTP 接口按需取段落，
> 供 AI agent / RAG 使用；全程本地、只读、无遥测。

---

## Why

- LLM context is expensive. A whole paper is often 10k–40k tokens; the 5 passages that answer a
  question are usually < 3k tokens.
- Full-text search inside Zotero is item-level, not passage-level, and it returns no stable
  machine-readable contract for agents.
- Generic vector databases require exporting your library somewhere. This plugin keeps the index
  next to your Zotero profile and serves it over `127.0.0.1`.

## Features

| Feature | Detail |
|---|---|
| **Page-aware chunks** | Text is extracted with Zotero's own `PDFWorker.getFullText()`; every result carries a real page number. |
| **Hybrid retrieval** | BM25 (via `Intl.Segmenter`, handles Chinese without a dictionary) + optional dense vectors (`bge-m3` via a local Ollama). Reciprocal-rank fusion, tunable. |
| **Zero heavy dependencies** | No bundled PDF parser, no bundled thesaurus, no network calls. |
| **Read-only** | The index lives in its own directory under the Zotero profile; your library is never modified. |
| **Agent protocol** | A ready-to-paste protocol describing how an agent should read the evidence and avoid overclaiming (`/retrieval/protocol`, see [docs/agent-usage-protocol.md](docs/agent-usage-protocol.md)). |
| **Optional reranker** | A small local GPU/CPU cross-encoder service (`tools/rerank_service.py`) can reorder the fused candidates. |
| **Optional table sidecars** | Tables extracted out-of-band can be appended as supplementary segments (never used for ranking). |
| **OCR sidecar support** | Scanned PDFs can be OCR'ed into a sidecar directory and folded into the index. |

## Requirements

- **Zotero 7 or newer** (tested with Zotero 10.0.6).
- Windows, macOS or Linux (the plugin itself is cross-platform; helper scripts are primarily
  written for Windows/PowerShell, with Python tools being portable).
- *Optional:* [Ollama](https://ollama.com) with the `bge-m3` model for dense retrieval
  (`ollama pull bge-m3`, ~1.2 GB). Without it the plugin falls back to BM25 only.
- *Optional:* Python 3.10+ for the offline helpers (`tools/`), and a CUDA-capable GPU for the
  optional reranker.

## Install

1. Download the latest `retrieval-index-*.xpi` from [Releases](https://github.com/ZJW11398925/zotero-retrieval-index/releases).
2. In Zotero: **Tools → Add-ons → gear icon → Install Add-on From File…**, choose the `.xpi`,
   then restart Zotero.
   - The build is unsigned. In Zotero's Advanced → Config Editor set
     `xpinstall.signatures.required = false`.
3. In Zotero, open **Tools → Retrieval Index → Build index**. The first build embeds all chunks
   if Ollama is available; otherwise it builds a BM25-only index.

The index is stored at `<your Zotero profile>/retrieval-index/` and can be rebuilt or deleted at
any time.

### Enable automatic incremental updates

After the first manual build, new items are picked up automatically when Zotero starts
(90 s delay) and then periodically:

```
extensions.zotero.retrieval.autoIncremental = true
extensions.zotero.retrieval.autoIncrementalDelaySec = 90
extensions.zotero.retrieval.autoIncrementalMinutes = 60   # 0 = only at startup
```

Set them in **Settings → Advanced → Config Editor**, or through the tuning API described below.

## HTTP API

The plugin registers endpoints on Zotero's built-in local server (default `127.0.0.1:23119`).
All endpoints are read-only except `/retrieval/build` and the tuning admin endpoint.

| Endpoint | Method | Description |
|---|---|---|
| `/retrieval/search?q=<text>&k=<n>` | GET | Hybrid search. Returns `{evidence, results}`; each result has `itemKey`, `title`, `page`, `pageSource`, `role`, `text`. |
| `/retrieval/stats` | GET | Index statistics (chunks, papers, dimensions, build time). |
| `/retrieval/ping` | GET | Liveness and plugin version. |
| `/retrieval/env` | GET | Environment report: Zotero version, libraries, Ollama status, index state, auto-incremental prefs. |
| `/retrieval/protocol` | GET | Markdown protocol for AI agents (how to read evidence, when to expand sub-queries). |
| `/retrieval/build?incremental=1` | GET | Trigger a (re)build; `incremental=1` reuses unchanged items. |
| `/retrieval/ocr-candidates` | GET | Items with no text layer / blank pages, for OCR helpers. |
| `/retrieval/admin/tune?key=<k>&value=<v>&confirm=TUNE` | GET | Adjust retrieval parameters at runtime (see below). |

Example:

```bash
curl --noproxy '*' "http://127.0.0.1:23119/retrieval/search?q=children%20color%20preference&k=5"
```

> On Windows, if a system proxy is configured, add `--noproxy '*'` for local requests.

### Tuning parameters

| Key | Meaning | Default |
|---|---|---|
| `rrfK` | RRF fusion constant | 30 |
| `wBm25` / `wDense` | Weight of each retrieval branch | 1 / 1 |
| `capPerPaper` | Max passages returned per paper | 2 |
| `abstractPrior` | Prefer abstract/homepage chunk (0/1) | 0 |
| `rerank` / `rerankN` / `rerankW` | Optional reranker switch, candidate count, blend weight | 0 / 10 / 0.6 |
| `tableSupplement` / `tableSupplementMax` | Append extracted table segments | 1 / 2 |
| `autoIncremental` / `autoIncrementalDelaySec` / `autoIncrementalMinutes` | Auto-index schedule | 1 / 90 / 0 |

## Agent usage

The intended consumer is an AI agent. Recommended flow:

1. Fetch `/retrieval/protocol` and include it in the agent's system prompt.
2. Call `/retrieval/search` with the user's question (`k` between 3 and 20).
3. Quote only what the returned passages support; use `page` to cite the exact page.
4. For enumerations/comparisons, use the protocol's sub-query expansion rules.

## Optional components

- **Ollama + bge-m3** — dense retrieval. Configure the endpoint with the
  `extensions.zotero.retrieval.ollamaURL` pref (default `http://127.0.0.1:11434`).
- **Reranker service** — `python tools/rerank_service.py` exposes `POST /rerank` on
  `127.0.0.1:8898`; point the plugin at it with the `rerank` pref. Any
  `bge-reranker-*` compatible model can be placed in `RERANK_MODEL_DIR`.
- **Table extraction** — `python tools/extract_tables.py` writes `tables/<itemKey>.json`
  sidecars into the index directory; `tools/fix_table_headers.py` and
  `tools/merge_tables.py` clean up multi-row headers and cross-page tables.
- **OCR sidecars** — `python tools/ocr_helper.py` uses platform OCR (Windows built-in) to
  extract text from scanned PDFs into `ocr/<itemKey>.json`.

## Development

```bash
# Build the plugin package (no external dependencies)
python build.py           # -> retrieval-index-<version>.xpi

# Unit tests (JS core: tokenizer, chunker, BM25)
node test/test_core.mjs   # set ZOTERO_EVAL_DIR to a fixture directory first

# Python helper syntax check
python -m compileall tools
```

Project layout:

```
content/retrieval.js      # plugin logic: build, search, server endpoints
content/core.js           # tokenizer, chunker, BM25, vector packing (unit-testable)
content/protocol-text.js  # generated from docs/agent-usage-protocol.md
bootstrap.js, prefs.js    # extension entry points and default prefs
build.py                  # packages the XPI
tools/                    # offline helpers (tables, OCR, reranker)
docs/                     # architecture and agent protocol
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and index format. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Privacy and security

- The index contains text extracted from **your own** documents. Keep the profile directory and
  any exported index private; do not commit it to a public repository.
- The plugin does not collect telemetry and makes no network requests except to your local Ollama
  instance (if enabled).
- The HTTP API binds to Zotero's local server. Do not expose it to untrusted networks; if you
  need remote access, place a local reverse proxy in front of it and restrict it to trusted hosts.
- Security issues: see [SECURITY.md](SECURITY.md).

## Limitations

- Page numbers are physical PDF pages, not printed page numbers.
- Scanned PDFs without a text layer need OCR sidecars; otherwise they are skipped.
- Figures/tables are only searchable to the extent their text exists in the PDF text layer or in
  table sidecars.
- Not affiliated with or endorsed by the Zotero project.

---

## 中文简介

本插件为 Zotero 本地文献库建立**带真实页码的段落索引**，并通过 Zotero 自带本地 HTTP 服务
（默认 `127.0.0.1:23119`）提供检索接口，供 AI agent / RAG 程序按需取用，避免把整篇论文塞入
上下文。支持 BM25（内置中文分词）+ 可选 bge-m3 混合检索、可选重排、表格补充段与扫描件 OCR
旁路。全程本地、只读、无遥测。

- 安装：从 Releases 下载 `.xpi` → 工具 → 插件 → Install Add-on From File → 重启；
  未签名构建需将 `xpinstall.signatures.required` 设为 `false`。
- 建索引：工具菜单 → Retrieval Index → 建立索引；新文献可通过自动增量参数自动收录。
- 使用协议：`curl --noproxy '*' http://127.0.0.1:23119/retrieval/protocol`
- 可选组件：Ollama + `bge-m3`（向量检索）、本地 reranker、表格/OCR 旁路（见 `tools/`）。

## License

[MIT](LICENSE) © 2026 ZJW11398925 and contributors.
