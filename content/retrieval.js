/**
 * Zotero 集成层（防御式）：任何一步失败都不影响插件加载与接口响应。
 * 依赖 content/core.js 先行加载（全局 RetrievalCore）。
 */
var RetrievalIndex = (function () {
	'use strict';

	const PREF = k => 'extensions.zotero.retrieval.' + k;
	const getPref = (k, d) => {
		try {
			const v = Zotero.Prefs.get(PREF(k), true);
			return (v === undefined || v === null) ? d : v;
		} catch (e) { return d; }
	};

	let rootURI = '';
	let dataDir = '';
	let cached = null;
	let bm25 = null;
	let building = false;
	let cancelRequested = false;
	let menuItems = [];
	let progressWin = null;
	let startupError = null;
	let buildState = { running: false, phase: 'idle', processed: 0, total: 0, chunks: 0, failed: 0, startedAt: null, error: null };

	function log(msg) { try { Zotero.debug('[retrieval] ' + msg); } catch (e) {} }

	// 撤稿文献：标题通常以 "Retracted:" / "Retraction Note" 开头或包含该标记
	const RETRACTED_RE = /(^|\s|\[|\()(retracted|retraction note|withdrawn)(\s|:|\]|\))/i;
	function isRetractedTitle(title) {
		return !!title && RETRACTED_RE.test(String(title));
	}

	function profilePath() {
		const f = Services.dirsvc.get('ProfD', Ci.nsIFile);
		return f.path;
	}

	function computeDataDir() {
		try {
			const base = profilePath();
			return base.replace(/[\\\/]+$/, '') + '\\retrieval-index';
		} catch (e) {
			return '';
		}
	}

	function idxPath(name) {
		return dataDir ? (dataDir + '\\' + name) : '';
	}

	async function readText(name) {
		const p = idxPath(name);
		if (!p) return null;
		try { return await Zotero.File.getContentsAsync(p); }
		catch (e) { return null; }
	}

	async function readAbs(path) {
		try { return await Zotero.File.getContentsAsync(path); }
		catch (e) { return null; }
	}

	async function writeText(name, text) {
		const p = idxPath(name);
		if (!p) throw new Error('索引目录未就绪');
		try { await Zotero.File.putContentsAsync(p, text); }
		catch (e) { throw new Error('写入失败 ' + p + '：' + (e.message || e)); }
	}

	// ---------------- 持久化 ----------------
	async function saveIndex(chunks, vecData, dim, meta) {
		if (!dataDir) throw new Error('索引目录未就绪');
		let vecPart = '';
		if (vecData && vecData.length) {
			const q = new Int8Array(vecData.length);
			for (let i = 0; i < vecData.length; i++) {
				const v = Math.max(-1, Math.min(1, vecData[i]));
				q[i] = Math.round(v * 127);
			}
			// Int8Array 含负值，必须先转成无符号字节再转字符串，否则 btoa 会因 >255 报错
			const u8 = new Uint8Array(q.buffer, q.byteOffset, q.length);
			let bin = '';
			const step = 0x8000;
			for (let i = 0; i < u8.length; i += step) {
				bin += String.fromCharCode.apply(null, u8.subarray(i, i + step));
			}
			vecPart = btoa(bin);
		}
		await writeText('chunks.json', JSON.stringify(chunks));
		await writeText('vectors.b64', vecPart);
		await writeText('meta.json', JSON.stringify(Object.assign({}, meta, { dim: dim || 0, count: chunks.length })));
		log('索引已保存：' + chunks.length + ' 块');
	}

	async function loadIndex() {
		if (cached) return cached;
		const metaRaw = await readText('meta.json');
		if (!metaRaw) { cached = null; return null; }
		const meta = JSON.parse(metaRaw);
		const chunksRaw = await readText('chunks.json');
		if (!chunksRaw) { cached = null; return null; }
		const chunks = JSON.parse(chunksRaw);
		let matrix = null;
		const dim = meta.dim || 0;
		const b64 = await readText('vectors.b64');
		if (b64 && dim) {
			try {
				const bin = atob(b64);
				const q = new Int8Array(bin.length);
				for (let i = 0; i < bin.length; i++) q[i] = bin.charCodeAt(i);
				matrix = new Float32Array(q.length);
				for (let i = 0; i < q.length; i++) matrix[i] = q[i] / 127;
			} catch (e) { log('向量解码失败：' + e); matrix = null; }
		}
		// 摘要页先验：保存每篇文献**所有**首页块的索引（首页常被切成标题/作者/摘要/引言等多块，
		// 检索时取其中排名最靠前的一块作代表，而不是取最长的一块——后者排名可能很靠后）
		const frontMap = new Map();
		for (let i = 0; i < chunks.length; i++) {
			const c = chunks[i];
			if (c.role !== 'front') continue;
			const k = titleKeyOf(c.title) || c.parentKey || c.itemKey;
			let arr = frontMap.get(k);
			if (!arr) { arr = []; frontMap.set(k, arr); }
			arr.push(i);
		}
		cached = { chunks, matrix, dim, meta, frontMap };
		log('索引已加载：' + chunks.length + ' 块' + (matrix ? '（含向量）' : '（无向量）') + '，摘要页 ' + frontMap.size + ' 篇');
		return cached;
	}

	function getBm25() {
		if (bm25 || !cached) return bm25;
		const docs = cached.chunks.map(c => RetrievalCore.tokenize(c.text));
		bm25 = RetrievalCore.buildBm25(docs);
		return bm25;
	}

	// ---------------- Ollama ----------------
	// Zotero 插件沙箱里可能没有 AbortController / setTimeout，统一做降级保护
	function timeoutSignal(ms) {
		try {
			if (typeof AbortController === 'undefined' || typeof setTimeout === 'undefined') {
				return { signal: undefined, clear: function () {} };
			}
			const ac = new AbortController();
			const timer = setTimeout(function () { ac.abort(); }, ms);
			return { signal: ac.signal, clear: function () { clearTimeout(timer); } };
		} catch (e) {
			return { signal: undefined, clear: function () {} };
		}
	}

	function ollamaBase() {
		return String(getPref('ollamaURL', 'http://127.0.0.1:11434')).replace(/\/+$/, '');
	}

	async function embedBatch(texts) {
		const res = await fetch(ollamaBase() + '/api/embed', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ model: getPref('model', 'bge-m3'), input: texts }),
			redirect: 'error'
		});
		if (!res.ok) throw new Error('Ollama HTTP ' + res.status);
		const j = await res.json();
		if (!j.embeddings || j.embeddings.length !== texts.length) throw new Error('向量数量不符');
		return j.embeddings;
	}

	// 分批 + 重试 + 逐个兜底；失败的位返回 null（调用方补零向量，保证与 chunks 对齐）
	async function embedSafe(texts, chunk = 32) {
		const out = new Array(texts.length).fill(null);
		for (let i = 0; i < texts.length; i += chunk) {
			const slice = texts.slice(i, i + chunk);
			let done = false;
			for (let attempt = 0; attempt < 2 && !done; attempt++) {
				try {
					const e = await embedBatch(slice);
					for (let j = 0; j < e.length; j++) out[i + j] = e[j];
					done = true;
				} catch (err) {
					if (attempt === 1) {
						for (let j = 0; j < slice.length; j++) {
							try { out[i + j] = await embedOne(slice[j]); } catch (e2) { out[i + j] = null; }
						}
						done = true;
					}
				}
			}
		}
		return out;
	}

	async function embedOne(text) {
		const ts = timeoutSignal(20000);
		try {
			const res = await fetch(ollamaBase() + '/api/embed', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ model: getPref('model', 'bge-m3'), input: [text] }),
				signal: ts.signal, redirect: 'error'
			});
			if (!res.ok) throw new Error('HTTP ' + res.status);
			const j = await res.json();
			return j.embeddings[0];
		} finally { ts.clear(); }
	}

	async function ollamaAvailable() {
		const ts = timeoutSignal(4000);
		try {
			const res = await fetch(ollamaBase() + '/api/tags', { signal: ts.signal });
			return res.ok;
		} catch (e) { log('Ollama 探测失败：' + (e && e.message || e)); return false; }
		finally { ts.clear(); }
	}

	// ---------------- 进度 ----------------
	function ui(headline, text) {
		try {
			if (!progressWin) { progressWin = new Zotero.ProgressWindow(); progressWin.changeHeadline(headline); }
			progressWin.addDescription(text);
			progressWin.show();
		} catch (e) { log(text); }
	}

	function uiClose() {
		try { if (progressWin) { progressWin.startCloseTimer(4000); progressWin = null; } } catch (e) {}
	}

	// ---------------- 建索引 ----------------
	async function build({ limit = 0, incremental = 0, embedOnly = 0 } = {}) {
		if (building) throw new Error('已有建索引任务在运行');
		building = true;
		cancelRequested = false;
		buildState = { running: true, phase: 'scanning', processed: 0, total: 0, chunks: 0, failed: 0, startedAt: Date.now(), error: null };
		const t0 = Date.now();
		try {
			// embedOnly：只给现有 chunks 补向量，不重抽文本
			if (embedOnly) {
				buildState.phase = 'embedding';
				const chunksRaw = await readText('chunks.json');
				if (!chunksRaw) throw new Error('没有现成索引可补向量');
				const ex = JSON.parse(chunksRaw);
				buildState.total = ex.length;
				const doEmbed = await ollamaAvailable();
				if (!doEmbed) throw new Error('Ollama 不可用，无法补向量');
				const dimProbe = parseInt(getPref('embedDim', 1024), 10) || 1024;
				const vecs = [];
				let embedErrors = 0, doneE = 0;
				const BATCH = 128;
				for (let i = 0; i < ex.length; i += BATCH) {
					if (cancelRequested) break;
					const slice = ex.slice(i, i + BATCH);
					const embs = await embedSafe(slice.map(c => c.text || ''));
					for (const e of embs) {
						if (e && e.length) vecs.push(e);
						else { vecs.push(new Array(dimProbe).fill(0)); embedErrors++; }
					}
					doneE += slice.length;
					buildState.processed = doneE;
					buildState.phase = 'embedding';
					if (Math.floor(doneE / BATCH) % 10 === 0) {
						ui('补向量', doneE + '/' + ex.length);
						await Zotero.Promise.delay(0);
					}
				}
				const packed = RetrievalCore.packVectors(vecs);
				await writeText('vectors.b64', (function () {
					const q = new Int8Array(packed.data.length);
					for (let i = 0; i < packed.data.length; i++) {
						const v = Math.max(-1, Math.min(1, packed.data[i]));
						q[i] = Math.round(v * 127);
					}
					const u8 = new Uint8Array(q.buffer, q.byteOffset, q.length);
					let bin = '';
					for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
					return btoa(bin);
				})());
				const metaRaw2 = await readText('meta.json');
				const meta2 = metaRaw2 ? JSON.parse(metaRaw2) : {};
				meta2.dim = packed.dim;
				meta2.embedErrors = embedErrors;
				meta2.embedOnlyAt = new Date().toISOString();
				await writeText('meta.json', JSON.stringify(meta2));
				cached = null; bm25 = null;
				const secsE = ((Date.now() - t0) / 1000).toFixed(0);
				buildState = Object.assign({}, buildState, { running: false, phase: 'done', chunks: ex.length, seconds: +secsE, hasVectors: true });
				ui('补向量完成', ex.length + ' 块，用时 ' + secsE + ' 秒' + (embedErrors ? '，失败 ' + embedErrors : ''));
				uiClose();
				return { chunks: ex.length, embedErrors, seconds: +secsE, embedOnly: true };
			}
			// 扫描全部可访问库（个人库 + 群组库），否则会漏掉群组库里的文献
			const libs = Zotero.Libraries.getAll().filter(l => l.libraryType === 'user' || l.libraryType === 'group');
			const libraries = [];
			let ids = [];
			for (const lib of libs) {
				try {
					const s = new Zotero.Search();
					s.libraryID = lib.libraryID;
					s.addCondition('itemType', 'is', 'attachment');
					const got = await s.search();
					ids.push(...got.map(id => ({ id, libraryID: lib.libraryID })));
					libraries.push({ id: lib.libraryID, name: lib.name, type: lib.libraryType, attachments: got.length });
				} catch (e) {
					log('扫描库 ' + lib.libraryID + ' 失败：' + e);
				}
			}
			if (limit > 0) ids = ids.slice(0, limit);
			buildState.total = ids.length;
			const maxPages = parseInt(getPref('maxPagesPerItem', 80), 10);
			const chunkChars = parseInt(getPref('chunkChars', 520), 10);
			const doEmbed = await ollamaAvailable();
			if (!doEmbed) log('未检测到 Ollama，本次仅建立 BM25 索引（探测 ' + ollamaBase() + '/api/tags；若本机开了全局代理，需把 127.0.0.1 加入代理例外）');
			ui('建立检索索引', '共 ' + ids.length + ' 个附件（' + libraries.map(l => l.name + ':' + l.attachments).join('，') + '）；向量化：' + (doEmbed ? '开启' : '关闭'));

			const chunks = [];
			const vecs = [];
			let done = 0, used = 0, failed = 0, truncated = 0, embedErrors = 0, dimOutProbe = 0;
			let cacheRecovered = 0, retractedCount = 0, reused = 0, ocrUsed = 0;
			const itemVersions = {};
			const failedItems = [];
			const pageGaps = {};
			const ocrDir = dataDir + '\\ocr';
			const ocrUsedKeys = [];
			// 只读一次清单，避免对上千个条目逐个试读旁路文件
			const ocrManifestRaw = await readAbs(ocrDir + '\\_manifest.json');
			let ocrKeys = new Set();
			if (ocrManifestRaw) {
				try {
					const mj = JSON.parse(ocrManifestRaw);
					ocrKeys = new Set((mj && mj.keys) || []);
					log('OCR 旁路可用：' + ocrKeys.size + ' 篇');
				} catch (e) { log('OCR 清单解析失败：' + e); }
			}

			// 增量模式：读取旧索引，按条目版本号复用未变更的块与向量
			let prev = null;
			if (incremental) {
				try {
					const metaRaw = await readText('meta.json');
					const chunksRaw = await readText('chunks.json');
					if (metaRaw && chunksRaw) {
						const pm = JSON.parse(metaRaw);
						const pc = JSON.parse(chunksRaw);
						let rows = null;
						const b64 = await readText('vectors.b64');
						if (b64 && pm.dim) {
							const bin = atob(b64);
							rows = new Int8Array(bin.length);
							for (let i = 0; i < bin.length; i++) rows[i] = bin.charCodeAt(i);
						}
						const byItem = new Map();
						for (let i = 0; i < pc.length; i++) {
							const c = pc[i];
							if (!byItem.has(c.itemID)) byItem.set(c.itemID, []);
							byItem.get(c.itemID).push(i);
						}
						prev = { meta: pm, chunks: pc, rows, dim: pm.dim || 0, versions: pm.itemVersions || {}, byItem };
						log('增量模式：旧索引 ' + pc.length + ' 块，可用向量 ' + (rows ? '有' : '无'));
					}
				} catch (e) { log('增量读取失败，退回全量：' + e); prev = null; }
			}
			// 有旧向量才能复用（否则复用块会缺向量）；doEmbed 关闭时不需要向量
			const canReuse = !!(prev && prev.rows && prev.dim) || !!(prev && !doEmbed);

			for (const rec of ids) {
				const id = rec.id;
				if (cancelRequested) { log('用户取消'); break; }
				const item = Zotero.Items.get(id);
				if (!item) { done++; continue; }
				used++;
				// 孤立 PDF 附件（无父条目）也索引：以文件名为标题
				const isOrphan = !item.parentItemID;
				if (isOrphan && !item.isPDFAttachment()) { done++; continue; }
				let parent = null, title = '';
				try {
					if (item.parentItemID) {
						parent = Zotero.Items.get(item.parentItemID);
						title = parent ? (parent.getField('title') || '') : '';
					}
					if (!title) {
						title = item.getFilename ? (item.getFilename() || '') : (item.getField('title') || '');
						title = String(title).replace(/\.pdf$/i, '');
					}
				} catch (e) {}
				// 增量：条目版本未变则直接复用旧块与旧向量
				const ver = item.version || 0;
				itemVersions[id] = ver;
				const hasOcr = ocrKeys.has(item.key);
				const prevOcrUsed = (prev && prev.meta && prev.meta.ocrUsedKeys) || [];
				const ocrNew = hasOcr && prevOcrUsed.indexOf(item.key) === -1;
				if (prev && canReuse && !ocrNew && prev.versions[id] === ver && prev.byItem.has(id)) {
					for (const idx of prev.byItem.get(id)) {
						const oc = prev.chunks[idx];
						chunks.push({
							cid: oc.cid, itemKey: oc.itemKey, itemID: oc.itemID,
							parentKey: oc.parentKey || '', libraryID: oc.libraryID, title: oc.title,
							retracted: !!oc.retracted, page: oc.page, pageSource: oc.pageSource,
							role: oc.role || ((oc.page === 1) ? 'front' : ''),
							text: oc.text
						});
						if (doEmbed && prev.dim) {
							const row = new Float32Array(prev.dim);
							for (let j = 0; j < prev.dim; j++) row[j] = prev.rows[idx * prev.dim + j] / 127;
							vecs.push(row);
						}
					}
					reused++; done++;
					buildState.processed = done;
					buildState.chunks = chunks.length;
					buildState.phase = 'reusing';
					continue;
				}
				// 0) OCR 旁路优先（外部脚本生成，保留页分隔）
				let pages = [];
				let pageSource = 'pdf';
				let ocrPageSet = null;
				if (hasOcr) {
					const sraw = await readAbs(ocrDir + '\\' + item.key + '.json');
					if (sraw) {
						try {
							const sj = JSON.parse(sraw);
							if (sj && Array.isArray(sj.pages) && sj.pages.length) {
								pages = sj.pages.map(x => String(x || ''));
								pageSource = 'ocr';
								ocrPageSet = new Set(sj.ocrPages || []);
								ocrUsed++;
								ocrUsedKeys.push(item.key);
							}
						} catch (e) { log('OCR 旁路解析失败 ' + item.key + '：' + e); }
					}
				}
				// 1) 否则用 PDFWorker 逐页提取（有真实页码）
				if (!pages.length && item.isPDFAttachment()) {
					try {
						const res = await Zotero.PDFWorker.getFullText(id, maxPages);
						if (res && res.text) {
							if (res.totalPages > maxPages) truncated++;
							pages = String(res.text).split('\f');
						}
					} catch (e) { log('PDF 抽取失败 ' + item.key + '：' + (e && e.message || e)); }
				}
				// 2) 回退：读 Zotero 全文缓存（无页码，但能救回 PDF 缺失/损坏的文献）
				if (!pages.length) {
					try {
						const cachePath = Zotero.getStorageDirectory().path.replace(/[\\\/]+$/, '')
							+ '\\' + item.key + '\\.zotero-ft-cache';
						const txt = await Zotero.File.getContentsAsync(cachePath);
						if (txt && txt.length > 200) {
							pages = String(txt).split('\f');
							pageSource = 'cache';
							cacheRecovered++;
						}
					} catch (e) { /* 无缓存 */ }
				}
				if (!pages.length) {
					failed++; done++;
					if (failedItems.length < 300) failedItems.push({ itemID: id, itemKey: item.key, title, reason: 'no_text', orphan: isOrphan });
					continue;
				}
				if (pageSource !== 'ocr') {
					const gaps = [];
					for (let gi = 0; gi < pages.length; gi++) {
						if ((pages[gi] || '').replace(/\s/g, '').length < 50) gaps.push(gi + 1);
					}
					if (gaps.length && gaps.length < pages.length) pageGaps[item.key] = gaps;
				}
				// 缓存回退且无分页符时，页码不可知
				const pageUnknown = pageSource === 'cache' && pages.length === 1;
				const parentKey = parent ? parent.key : '';
				const retracted = isRetractedTitle(title);
				if (retracted) retractedCount++;
				let cs = [];
				try { cs = RetrievalCore.chunkPages(pages, { chunkChars, overlapChars: 80, minChunk: 120 }); }
				catch (e) { failed++; done++; continue; }
				for (let i = 0; i < cs.length; i++) {
					chunks.push({
						cid: item.key + '#' + cs[i].page + '#' + i, itemKey: item.key, itemID: id,
						parentKey, libraryID: rec.libraryID, title, retracted, orphan: isOrphan,
						page: pageUnknown ? 0 : cs[i].page,
						pageSource,
						role: (!pageUnknown && cs[i].page === 1) ? 'front' : '',
						ocr: !!(ocrPageSet && ocrPageSet.has(cs[i].page)),
						text: cs[i].text
					});
				}
				if (doEmbed && cs.length) {
					try {
						const embs = await embedSafe(cs.map(c => c.text));
						for (const e of embs) {
							if (e && e.length) { dimOutProbe = e.length; vecs.push(e); }
							else { vecs.push(new Array(dimOutProbe || 1024).fill(0)); embedErrors++; }
						}
					} catch (e) { embedErrors++; log('向量化失败：' + e); }
				}
				done++;
				buildState.processed = done;
				buildState.chunks = chunks.length;
				buildState.failed = failed;
				buildState.phase = 'extracting';
				if (done % 20 === 0) {
					ui('建立检索索引', done + '/' + ids.length + ' 附件，' + chunks.length + ' 块');
					await Zotero.Promise.delay(0);
				}
			}

			let vecData = null, dimOut = 0;
			if (vecs.length && vecs.length === chunks.length) {
				const packed = RetrievalCore.packVectors(vecs);
				vecData = packed.data; dimOut = packed.dim;
			} else if (vecs.length && chunks.length && vecs.length < chunks.length) {
				// 补齐缺向量的块为 0，保证与 chunks 对齐（仍可用 BM25 命中）
				const dimNow = vecs[0] ? vecs[0].length : 1024;
				const pad = chunks.length - vecs.length;
				for (let i = 0; i < pad; i++) vecs.push(new Array(dimNow).fill(0));
				embedErrors += pad;
				const packed = RetrievalCore.packVectors(vecs);
				vecData = packed.data; dimOut = packed.dim;
				log('向量不齐，已补零 ' + pad + ' 条');
			}
			await saveIndex(chunks, vecData, dimOut, {
				builtAt: new Date().toISOString(), libraries, model: getPref('model', 'bge-m3'),
				ollamaOnline: doEmbed, embedErrors, embedDim: dimOutProbe,
				cacheRecovered, retractedPapers: retractedCount, reused, itemVersions,
				ocrUsed, ocrUsedKeys, failedItems, pageGaps,
				sourceAttachments: used, failed, truncated, elapsedMs: Date.now() - t0
			});
			cached = null; bm25 = null; parentKeyCache.clear();
			const secs = ((Date.now() - t0) / 1000).toFixed(0);
			buildState = Object.assign({}, buildState, { running: false, phase: 'done', processed: done, chunks: chunks.length, failed, cacheRecovered, retractedCount, seconds: +secs, hasVectors: !!vecData });
			ui('建立检索索引', '完成：' + chunks.length + ' 块 / ' + used + ' 篇，用时 ' + secs + ' 秒' + (cacheRecovered ? '，缓存回填 ' + cacheRecovered : ''));
			uiClose();
			return { chunks: chunks.length, attachments: used, failed, truncated, cacheRecovered, retractedCount, reused, seconds: +secs };
		} catch (e) {
			buildState = Object.assign({}, buildState, { running: false, phase: 'error', error: String((e && e.message) || e) });
			throw e;
		} finally {
			building = false;
			uiClose();
		}
	}

	// 孤立 PDF 以文件名当标题，常带编号/作者/翻译后缀等噪声，检索时清洗
	function cleanTitle(title) {
		let s = String(title || '').trim();
		if (!s) return '';
		s = s.replace(/\.pdf$/i, '');
		s = s.replace(/^\s*\d{1,4}\s*[.、,_\-—]\s*/, '');        // 前导编号 "10." "29、"
		s = s.replace(/^[0-9_\-]{6,}[\s_]+/, '');                 // 前导长数字串 "200000505267_..."
		s = s.replace(/[_\-\s]*(翻译版|中译版|translate|translated|zh|中文版)\s*$/i, '');
		s = s.replace(/_v\d+$/i, '');
		s = s.replace(/[_\s]+[\u4e00-\u9fff]{2,4}$/, (m) => (m.length <= 5 ? '' : m)); // 尾部作者名（如 _温雅俊）
		s = s.replace(/\s{2,}/g, ' ').trim();
		return s || String(title || '');
	}

	// 标题信息量过低（纯编号、纯英文缩写等）→ 检索时降级
	function titlePoor(title) {
		const s = cleanTitle(title);
		const cjk = (s.match(/[\u4e00-\u9fff]/g) || []).length;
		const latin = (s.match(/[A-Za-z]/g) || []).length;
		return (cjk < 4 && latin < 12);
	}

	// 标题归一化：库里存在“同一文献被建成多个独立条目”的情况（实测 30 组），
	// 仅按父条目去重拦不住，需要再加一层标题级去重
	function titleKeyOf(title) {
		return String(title || '').toLowerCase()
			.replace(/[\s\u3000\u00a0]+/g, '')
			.replace(/[\-—–_,，。.、;；:：!！?？"'"'“”‘’()（）\[\]【】<>《》]/g, '');
	}

	// 用于对旧索引按需解析父条目（旧索引没有 parentKey 字段）
	const parentKeyCache = new Map();
	function resolveParentKey(c) {
		if (c.parentKey) return c.parentKey;
		const id = c.itemID;
		if (!id) return c.itemKey;
		if (parentKeyCache.has(id)) return parentKeyCache.get(id);
		let pk = c.itemKey;
		try {
			const it = Zotero.Items.get(id);
			if (it && it.parentItemID) {
				const p = Zotero.Items.get(it.parentItemID);
				if (p) pk = p.key;
			}
		} catch (e) {}
		parentKeyCache.set(id, pk);
		return pk;
	}

	// ---------------- 检索 ----------------
	async function search({ q, k = 8, mode = 'hybrid', includeRetracted = 0, full = 0, includeOrphans = null } = {}) {
		if (!q || !String(q).trim()) throw new Error('缺少查询词 q');
		const idx = await loadIndex();
		if (!idx || !idx.chunks.length) throw new Error('尚未建立索引');
		const bm = getBm25();
		const kk = Math.max(1, Math.min(50, parseInt(k, 10) || 8));
		let useOrphans = true;
		try {
			const v = Zotero.Prefs.get(PREF('includeOrphans'), true);
			if (v !== undefined && v !== null) useOrphans = !!v;
		} catch (e) {}
		if (includeOrphans !== null && includeOrphans !== undefined && includeOrphans !== '') {
			useOrphans = !(includeOrphans === 0 || includeOrphans === '0' || includeOrphans === false);
		}
		const qTokens = RetrievalCore.tokenize(String(q));
		const qSet = new Set(qTokens);
		const rk = [];
		rk.push(RetrievalCore.topN(RetrievalCore.bm25Score(qTokens, bm), 200));
		if (mode !== 'bm25' && idx.matrix && idx.dim) {
			try {
				const dn = RetrievalCore.denseScores(await embedOne(String(q)), idx.matrix, idx.dim);
				rk.push(RetrievalCore.topN(dn, 200));
			} catch (e) { log('查询向量失败：' + e); }
		}
		// RRF 融合 + 标题命中加权（改善长问句召回）
		const scoreMap = new Map();
		const K_RRF = 60;
		for (const rl of rk) {
			for (let i = 0; i < rl.length; i++) {
				scoreMap.set(rl[i], (scoreMap.get(rl[i]) || 0) + 1 / (K_RRF + i + 1));
			}
		}
		// 注意：曾尝试叠加“标题命中”绝对加分，实测是严重倒退——
		// RRF 单路 rank1 仅 1/(60+1)≈0.016，任何绝对加分都会把“标题沾边但正文无关”的文献顶到最前。
		// 离线评估（34 条金标）：加 0.25 后自然语言 R@5 从 93.8% 掉到 56.3%，故彻底移除。
		const ranked = [];
		for (const [i, base] of scoreMap) ranked.push([i, base]);
		ranked.sort((a, b) => b[1] - a[1]);
		const posOf = new Map();
		for (let r = 0; r < ranked.length && r < 200; r++) posOf.set(ranked[r][0], r);

		const out = [];
		const perPaper = new Map();          // 按父条目去重（同一文献多个 PDF 副本）
		const seenText = new Set();          // 完全相同的片段只留一条
		let skippedRetracted = 0, skippedOrphans = 0;
		for (let pass = 1; pass <= 2; pass++) {
		for (const [i] of ranked) {
			if (out.length >= kk) break;
			const c = idx.chunks[i];
			if (!c) continue;
			const isRet = c.retracted !== undefined ? !!c.retracted : isRetractedTitle(c.title);
			if (isRet && !includeRetracted) { skippedRetracted++; continue; }
			if (!useOrphans && c.orphan) { skippedOrphans++; continue; }
			const pk = resolveParentKey(c);
			const ctitle = cleanTitle(c.title);
			if (pass === 1 && titlePoor(c.title)) continue;   // 第一轮跳过低信息量标题
			const tk = titleKeyOf(ctitle) || pk;   // 同标题不同条目也视为同一文献
			const n = perPaper.get(tk) || 0;
			if (n >= 2) continue;
			// 摘要页先验：该文献首次出现时，若其首页块也在候选池前段，则用首页块作代表段
			// （实测案例：Barrett 2015 的综述段排第 1、摘要段仅排 18，导致 agent 拿综述段外推）
			// 摘要页先验（严格模式，只作用于首条结果）：
			// 实测 Barrett 2015 的综述段排第 1、真正含结果的摘要段排名很靠后，agent 只能拿综述段外推。
			// 只在**确实识别到摘要标记**时才替换——不做“退回任意首页块”的激进兜底
			// （实测中文文献首页块常无摘要标记，强行替换会把首条换成从句子中间开始的片段）。
			let emit = c;
			if (out.length === 0 && n === 0 && idx.frontMap && idx.frontMap.has(tk)) {
				const ABS = /摘\s*要|abstract/i;
				let pick = -1, bestAt = 1e9;
				for (const fi of idx.frontMap.get(tk)) {
					const fc = idx.chunks[fi];
					const txt = (fc && fc.text) || '';
					const m = txt.slice(0, 500).match(ABS);
					if (m && (m.index || 0) < bestAt) { bestAt = m.index || 0; pick = fi; }
				}
				if (pick >= 0 && pick !== i) {
					const fc = idx.chunks[pick];
					if (fc && fc.text) emit = fc;
				}
			}
			const tkey = (emit.text || '').slice(0, 120);
			if (seenText.has(tkey)) continue;
			seenText.add(tkey);
			perPaper.set(tk, n + 1);
			const ec = emit;
			out.push({
				itemKey: ec.itemKey, parentKey: resolveParentKey(ec), title: cleanTitle(ec.title),
				page: ec.page ? ec.page : null,
				pageSource: ec.pageSource || (ec.page ? 'pdf' : 'unknown'),
				role: ec.role || '',
				retracted: isRet,
				text: (ec.text || '').length > (full ? 3000 : 1200)
					? (ec.text || '').slice(0, full ? 3000 : 1200) + '…'
					: (ec.text || '')
			});
		}
		}
		if (skippedRetracted) log('已过滤撤稿文献 ' + skippedRetracted + ' 条（includeRetracted=1 可查看）');

		// 证据来源事实（不做“够不够”的判定——实测词面/余弦/本地小模型都不可靠，
		// 只给出可核验的来源信息，由调用方自行决定是否标注局限）
		const paperSet = new Set();
		let abstractOnly = 0, cacheSourced = 0, pageKnown = 0;
		for (const r of out) {
			paperSet.add(r.parentKey || r.itemKey);
			if (r.role === 'front') abstractOnly++;
			if (r.pageSource === 'cache') cacheSourced++;
			if (r.page) pageKnown++;
		}
		const evidence = {
			returned: out.length,
			papers: paperSet.size,
			abstractOnlySegments: abstractOnly,
			cacheSourcedSegments: cacheSourced,
			segmentsWithPage: pageKnown,
			topSegmentRole: out.length ? (out[0].role || 'body') : null,
			retractedFiltered: skippedRetracted,
			orphansIncluded: useOrphans,
			orphansFiltered: skippedOrphans,
			hints: []
		};
		if (out.length && evidence.segmentsWithPage < out.length) {
			evidence.hints.push('部分片段无页码（来自 Zotero 全文缓存，原文 PDF 缺失）');
		}
		if (out.length && abstractOnly === out.length) {
			evidence.hints.push('全部片段均为摘要/首页内容，未覆盖正文方法与结果');
		} else if (abstractOnly > 0) {
			evidence.hints.push('含 ' + abstractOnly + ' 条摘要/首页片段');
		}
		if (skippedRetracted) evidence.hints.push('已过滤 ' + skippedRetracted + ' 条撤稿文献');
		return { results: out, evidence };
	}

	function stats() {
		const out = { build: buildState, dataDir, startupError };
		if (!cached) return Object.assign({ loaded: false }, out);
		const pks = new Set(), retSet = new Set();
		for (const c of cached.chunks) {
			pks.add(c.parentKey || c.itemKey);
			if (c.retracted !== undefined ? c.retracted : isRetractedTitle(c.title)) retSet.add(c.parentKey || c.itemKey);
		}
		return Object.assign({
			loaded: true, chunks: cached.chunks.length,
			papers: pks.size,
			retractedPapers: retSet.size,
			hasVectors: !!cached.matrix, dim: cached.dim, meta: cached.meta
		}, out);
	}

	// ---------------- 接口 ----------------
	function registerEndpoints() {
		// 纯同步诊断接口：不读磁盘，用于确认插件是否加载
		Zotero.Server.Endpoints['/retrieval/ping'] = function () {};
		Zotero.Server.Endpoints['/retrieval/ping'].prototype = {
			supportedMethods: ['GET'],
			init: function (options) {
				return [200, 'application/json', JSON.stringify({
					ok: true, plugin: 'retrieval-index', version: '0.1.17',
					dataDir, hasDataDir: !!dataDir, startupError, ts: new Date().toISOString()
				})];
			}
		};

		Zotero.Server.Endpoints['/retrieval/ollama'] = function () {};
		Zotero.Server.Endpoints['/retrieval/ollama'].prototype = {
			supportedMethods: ['GET'],
			init: async function (options) {
				const url = ollamaBase() + '/api/tags';
				const ts = timeoutSignal(8000);
				try {
					const res = await fetch(url, { signal: ts.signal });
					ts.clear();
					let models = [];
					try { const j = await res.json(); models = (j.models || []).map(m => m.name); } catch (e) {}
					return [200, 'application/json', JSON.stringify({ ok: res.ok, url, models })];
				} catch (e) {
					ts.clear();
					return [200, 'application/json', JSON.stringify({ ok: false, url, error: String((e && e.message) || e) })];
				}
			}
		};

		Zotero.Server.Endpoints['/retrieval/ocr-candidates'] = function () {};
		Zotero.Server.Endpoints['/retrieval/ocr-candidates'].prototype = {
			supportedMethods: ['GET'],
			init: async function (options) {
				try {
					await loadIndex();
					const m = (cached && cached.meta) || {};
					return [200, 'application/json', JSON.stringify({
						ocrDir: dataDir + '\\ocr',
						ocrUsed: m.ocrUsed || 0,
						failedCount: (m.failedItems || []).length,
						failed: m.failedItems || [],
						blankPageCount: Object.keys(m.pageGaps || {}).length,
						blankPages: m.pageGaps || {}
					})];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ error: String((e && e.message) || e) })];
				}
			}
		};

		Zotero.Server.Endpoints['/retrieval/stats'] = function () {};
		Zotero.Server.Endpoints['/retrieval/stats'].prototype = {
			supportedMethods: ['GET'],
			init: async function (options) {
				try {
					await loadIndex();
					return [200, 'application/json', JSON.stringify(stats())];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ error: String((e && e.message) || e), dataDir })];
				}
			}
		};

		Zotero.Server.Endpoints['/retrieval/search'] = function () {};
		Zotero.Server.Endpoints['/retrieval/search'].prototype = {
			supportedMethods: ['GET'],
			init: async function (o) {
				try {
					const r = await search({
						q: o.searchParams.get('q'),
						k: o.searchParams.get('k') || 8,
						mode: o.searchParams.get('mode') || 'hybrid',
						includeRetracted: o.searchParams.get('includeRetracted') || 0,
						full: o.searchParams.get('full') || 0,
						includeOrphans: o.searchParams.get('orphans')
					});
					const flat = parseInt(o.searchParams.get('flat') || '0', 10);
					return [200, 'application/json', JSON.stringify(flat ? r.results : r)];
				} catch (e) {
					return [400, 'application/json', JSON.stringify({ error: String((e && e.message) || e) })];
				}
			}
		};

		Zotero.Server.Endpoints['/retrieval/build'] = function () {};
		Zotero.Server.Endpoints['/retrieval/build'].prototype = {
			supportedMethods: ['GET', 'POST'],
			init: function (o) {
				try {
					if (building) return [200, 'application/json', JSON.stringify({ started: false, busy: true, build: buildState })];
					const limit = parseInt(o.searchParams.get('limit') || '0', 10) || 0;
					const incremental = parseInt(o.searchParams.get('incremental') || '0', 10) || 0;
					const embedOnly = parseInt(o.searchParams.get('embedonly') || '0', 10) || 0;
					build({ limit, incremental, embedOnly }).catch(e => log('建索引失败：' + e));
					return [200, 'application/json', JSON.stringify({ started: true, limit, incremental, embedOnly })];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ error: String((e && e.message) || e) })];
				}
			}
		};
		log('接口已注册');
	}

	function unregisterEndpoints() {
		for (const p of ['/retrieval/ping', '/retrieval/stats', '/retrieval/search', '/retrieval/build', '/retrieval/ollama', '/retrieval/ocr-candidates']) {
			try { delete Zotero.Server.Endpoints[p]; } catch (e) {}
		}
	}

	// ---------------- 菜单 ----------------
	function addMenuItems() {
		try {
			const win = Zotero.getMainWindow();
			const popup = win.document.getElementById('menu_ToolsPopup');
			if (!popup) { log('找不到工具菜单'); return; }
			const mk = (id, label, fn) => {
				const mi = win.document.createXULElement('menuitem');
				mi.id = 'retrieval-' + id;
				mi.setAttribute('label', label);
				mi.addEventListener('command', fn);
				popup.appendChild(mi);
				menuItems.push(mi);
			};
			mk('build', '建立检索索引', async () => {
				try {
					const r = await build({});
					Services.prompt.alert(null, '检索索引', '完成：' + r.chunks + ' 块 / ' + r.attachments + ' 篇附件' + (r.failed ? '，失败 ' + r.failed : '') + '，用时 ' + r.seconds + ' 秒');
				} catch (e) {
					Services.prompt.alert(null, '检索索引失败', String((e && e.message) || e));
				}
			});
			mk('build-inc', '增量更新索引（仅处理新增/变更）', async () => {
				try {
					const r = await build({ incremental: 1 });
					Services.prompt.alert(null, '增量更新',
						'完成：' + r.chunks + ' 块（复用 ' + (r.reused || 0) + ' 篇）'
						+ (r.failed ? '，失败 ' + r.failed : '') + '，用时 ' + r.seconds + ' 秒');
				} catch (e) {
					Services.prompt.alert(null, '增量更新失败', String((e && e.message) || e));
				}
			});
			mk('cancel', '取消建索引', () => {
				cancelRequested = true;
				Services.prompt.alert(null, '检索索引', '已请求取消。');
			});
			mk('stats', '检索索引状态', async () => {
				await loadIndex();
				const s = stats();
				Services.prompt.alert(null, '检索索引', s.loaded
					? ('块：' + s.chunks + '\n文献：' + s.papers + '\n向量：' + (s.hasVectors ? ('有（' + s.dim + ' 维）') : '无') + '\n建立时间：' + ((s.meta && s.meta.builtAt) || '未知'))
					: ('尚未建立索引\n数据目录：' + dataDir));
			});
			mk('ocr', 'OCR 候选清单', async () => {
				await loadIndex();
				const m = (cached && cached.meta) || {};
				const nf = (m.failedItems || []).length;
				const nb = Object.keys(m.pageGaps || {}).length;
				Services.prompt.alert(null, 'OCR 候选',
					'完全无文本的文献：' + nf + ' 篇\n含空白页的文献：' + nb + ' 篇\n已用 OCR 旁路：' + (m.ocrUsed || 0) + ' 篇\n\n旁路目录：\n' + dataDir + '\\ocr\n\n用 tools/ocr_helper.py 生成旁路文件后，点「增量更新索引」即可并入。');
			});
			mk('test', '检索自检（示例查询）', async () => {
				try {
					const r = await search({ q: '儿童色彩偏好', k: 3 });
					const list = r.results || [];
					Services.prompt.alert(null, '检索自检', list.map((x, i) => (i + 1) + '. [' + (x.page || '无页码') + '页] ' + (x.title || '').slice(0, 30) + '\n   ' + x.text.slice(0, 60) + '…').join('\n\n') || '无结果');
				} catch (e) {
					Services.prompt.alert(null, '检索自检失败', String((e && e.message) || e));
				}
			});
			log('菜单已注册');
		} catch (e) { log('菜单注册失败：' + e); }
	}

	function removeMenuItems() {
		for (const mi of menuItems) { try { mi.remove(); } catch (e) {} }
		menuItems = [];
	}

	// ---------------- 生命周期 ----------------
	async function init(opts) {
		try {
			rootURI = (opts && opts.rootURI) || '';
			dataDir = computeDataDir();
			startupError = null;
			registerEndpoints();               // 先保证接口可用
			try { await Zotero.File.createDirectoryIfMissingAsync(dataDir); }
			catch (e) { startupError = '创建数据目录失败：' + (e.message || e); log(startupError); }
			addMenuItems();
			log('初始化完成 dataDir=' + dataDir);
		} catch (e) {
			startupError = String((e && e.message) || e);
			log('初始化异常：' + startupError);
		}
	}

	function shutdown() {
		removeMenuItems();
		unregisterEndpoints();
		cached = null; bm25 = null;
	}

	return { init, shutdown, build, search, stats };
})();
