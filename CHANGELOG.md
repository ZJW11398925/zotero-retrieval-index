# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Cross-platform helper scripts for the optional tooling
- Better handling of image-only PDFs

## [0.1.52] — 2026-10-25

### Added

- `/retrieval/env` environment endpoint (Zotero version, libraries, Ollama status, index state,
  auto-incremental preferences).
- Optional bundle tooling for unattended installation (not part of the plugin runtime).

### Changed

- `autoIncremental`, `autoIncrementalDelaySec` and `autoIncrementalMinutes` are now part of the
  runtime tuning whitelist.

## [0.1.51] — 2026-10-25

### Added

- Runtime tuning for automatic incremental builds.

## [0.1.50] — 2026-10-25

### Changed

- Rebuilt snapshot after library growth; table sidecars refreshed.

## [0.1.49] — 2026-10-24

### Changed

- Development README is no longer packaged into the plugin XPI.
- Table sidecars store only the source file name, never absolute paths.

## [0.1.48] — 2026-10-24

### Added

- Table header normalization (multi-row headers) and cross-page table merging helpers.

## [0.1.47] — 2026-10-24

### Added

- Table sidecar support: tables extracted out-of-band are appended as supplementary segments
  (`evidence.tableSupplements`), without affecting ranking.
- Agent protocol documents table supplements.

## [0.1.46] — 2026-10-24

### Fixed

- Embedding inputs now include the paper title during full rebuilds, restoring retrieval quality.

## [0.1.42] — 2026-10-23

### Added

- Optional cross-encoder reranker service (`tools/rerank_service.py`).

## [0.1.36] — 2026-10-22

### Added

- Reranker integration (blend of RRF rank and cross-encoder score).

## [0.1.31] — 2026-10-21

### Changed

- Embedding context enrichment (title + chunk text) accepted as default.

## [0.1.27] — 2026-10-20

### Added

- Dense retrieval via local Ollama (`bge-m3`) with reciprocal-rank fusion.

## [0.1.21] — 2026-10-18

### Added

- Agent usage protocol endpoint (`/retrieval/protocol`).

## [0.1.13] — 2026-10-15

### Added

- OCR sidecar support for scanned documents.

## [0.1.0] — 2026-10-10

### Added

- First public preview: page-aware chunking, BM25 index, `/retrieval/search`,
  `/retrieval/stats`, `/retrieval/build`.

[Unreleased]: https://github.com/ZJW11398925/zotero-retrieval-index/compare/v0.1.52...HEAD
[0.1.52]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.52
[0.1.51]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.51
[0.1.50]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.50
[0.1.49]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.49
[0.1.48]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.48
[0.1.47]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.47
[0.1.46]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.46
[0.1.42]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.42
[0.1.36]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.36
[0.1.31]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.31
[0.1.27]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.27
[0.1.21]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.21
[0.1.13]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.13
[0.1.0]: https://github.com/ZJW11398925/zotero-retrieval-index/releases/tag/v0.1.0
