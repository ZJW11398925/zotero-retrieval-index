# Contributing

Thanks for your interest in Improving Retrieval Index! This project follows a lightweight,
review-driven workflow.

## Before you start

- Search existing [issues](https://github.com/ZJW11398925/zotero-retrieval-index/issues) and
  pull requests to avoid duplicate work.
- For non-trivial changes, open an issue first to discuss the design. This is especially
  important for changes to the retrieval pipeline, index format, or HTTP API.
- By contributing you agree that your contributions are licensed under the project's MIT license.

## Development setup

Requirements:

- Zotero 7+ (for manual testing)
- Node.js 18+ (for `content/core.js` unit tests and syntax checks)
- Python 3.10+ (optional; only for the helper tools in `tools/`)

```bash
git clone https://github.com/ZJW11398925/zotero-retrieval-index.git
cd zotero-retrieval-index
python build.py            # produces retrieval-index-<version>.xpi
node test/test_core.mjs    # requires a fixture directory, see below
```

### Test fixtures

`test/test_core.mjs` expects a directory pointed to by `ZOTERO_EVAL_DIR` containing:

- `chunks.jsonl` — one JSON object per chunk (`{pid, text, page}`), and
- `queries.json` — an array of `{q, gt}` query fixtures.

Without the environment variable the test reports the fixture directory is missing and exits
successfully, so CI can run the syntax/build checks without private data.

### Style

- JavaScript: 2-space indentation, semicolons, modern ES2018+ syntax (the code runs inside
  Zotero's Gecko engine).
- Python: PEP 8-ish, no third-party dependencies unless unavoidable.
- PowerShell: compatible with Windows PowerShell 5.1; UTF-8 files should be stored with a BOM.
- Keep user-visible strings in English or Chinese consistently within a file.

## Pull requests

1. Fork the repository and create a topic branch.
2. Keep changes focused; update documentation (README, `docs/`) when behavior changes.
3. Add or update tests where practical.
4. Ensure `node --check` passes for every JS file and `python -m compileall tools` succeeds.
5. Describe **what** changed and **why** in the PR template; reference related issues.

CI must pass before a maintainer reviews the change. Maintainers may ask for changes or close
PRs that do not fit the project's scope.

## Commit messages

Conventional-style prefixes are appreciated:

```
feat: add incremental rebuild for changed items
fix: avoid dropping chunks when a PDF has no text layer
docs: clarify the agent protocol
chore: bump version and rebuild xpi
```

## Reporting bugs and requesting features

Use the issue templates. For bugs, include:

- OS and Zotero version
- plugin version (`/retrieval/ping`)
- steps to reproduce
- relevant plugin debug output (Help → Debug Output Logging) — remove any private file paths
  or document text before posting.

## Security

Do not open public issues for security-sensitive reports. See [SECURITY.md](SECURITY.md).

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
