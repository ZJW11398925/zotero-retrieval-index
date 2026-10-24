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

	// v0.1.27：embedding 输入 = 论文标题 + 块原文（截断 900 字符），补齐块内缺失的上下文。
	// 离线验证（75 条金标，官方口径）：R@5 90.67%→92.00%、Hit@1 78.67%→80.00%、MRR +0.0139。
	// 需要用 embedOnly 重建一次向量；回滚：pref extensions.zotero.retrieval.embedTitle=0 后重建。
	function embedInputOf(c) {
		const text = String((c && c.text) || '');
		let withTitle = '1';
		try { withTitle = String(getPref('embedTitle', 1)); } catch (e) {}
		if (withTitle === '0' || withTitle === 'false') return text;
		const title = String((c && c.title) || '').trim();
		return (title ? (title + '\n' + text) : text).slice(0, 900);
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

	// 简单字符串哈希（用于 OCR 清单指纹）
	function hashStr(s) {
		let h = 5381;
		for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
		return h.toString(36);
	}

	function allowedLibraryIDs() {
		const raw = String(getPref('libraryIDs', '') || '').trim();
		if (!raw) return null;
		const set = new Set();
		for (const part of raw.split(/[,，\s]+/)) {
			const n = parseInt(part, 10);
			if (!isNaN(n)) set.add(n);
		}
		return set.size ? set : null;
	}

	// ---------------- 建索引 ----------------
	async function build({ limit = 0, incremental = 0, embedOnly = 0, silent = 0 } = {}) {
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
					const embs = await embedSafe(slice.map(embedInputOf));
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
			const allow = allowedLibraryIDs();
			const libs = Zotero.Libraries.getAll().filter(l =>
				(l.libraryType === 'user' || l.libraryType === 'group') && (!allow || allow.has(l.libraryID)));
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

			// OCR 清单参与指纹（避免只改了 OCR 却被判为无变更）
			const ocrManifestForFp = (await readAbs(dataDir + '\\ocr\\_manifest.json')) || '';
			// 快速指纹：附件数 + 版本和 + 最大版本 + OCR 清单哈希
			let verSum = 0, verMax = 0, verCount = 0;
			for (const rec of ids) {
				try {
					const it = Zotero.Items.get(rec.id);
					const v = (it && it.version) || 0;
					verSum += v; if (v > verMax) verMax = v; verCount++;
				} catch (e) {}
			}
			const fingerprint = [verCount, verSum, verMax, hashStr(ocrManifestForFp)].join('|');

			if (incremental) {
				const prevMetaRaw = await readText('meta.json');
				if (prevMetaRaw) {
					try {
						const pm = JSON.parse(prevMetaRaw);
						if (pm.fingerprint && pm.fingerprint === fingerprint) {
							log('指纹一致，无变更，跳过（' + fingerprint + '）');
							buildState = Object.assign({}, buildState, {
								running: false, phase: 'done', noChange: true, fingerprint,
								chunks: pm.count || 0, seconds: 0
							});
							return { chunks: pm.count || 0, noChange: true, seconds: 0 };
						}
					} catch (e) {}
				}
			}
			const maxPages = parseInt(getPref('maxPagesPerItem', 80), 10);
			const chunkChars = parseInt(getPref('chunkChars', 520), 10);
			const doEmbed = await ollamaAvailable();
			if (!doEmbed) log('未检测到 Ollama，本次仅建立 BM25 索引（探测 ' + ollamaBase() + '/api/tags；若本机开了全局代理，需把 127.0.0.1 加入代理例外）');
			if (!silent) ui('建立检索索引', '共 ' + ids.length + ' 个附件（' + libraries.map(l => l.name + ':' + l.attachments).join('，') + '）；向量化：' + (doEmbed ? '开启' : '关闭'));

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
						// 按 itemKey 分组（字符串 key 跨机器一致；itemID 是本机整数，不可移植）
						const byItem = new Map();
						for (let i = 0; i < pc.length; i++) {
							const c = pc[i];
							const k = c.itemKey || String(c.itemID);
							if (!byItem.has(k)) byItem.set(k, []);
							byItem.get(k).push(i);
						}
						const pv = pm.itemVersions || {};
						// 兼容旧索引（itemVersions 以数字 itemID 为键）
						const versionsByKey = {};
						for (const c of pc) {
							if (c.itemKey) versionsByKey[c.itemKey] = pv[c.itemID] !== undefined ? pv[c.itemID] : pv[c.itemKey];
						}
						prev = { meta: pm, chunks: pc, rows, dim: pm.dim || 0, versions: versionsByKey, byItem };
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
				itemVersions[item.key] = ver;
				const hasOcr = ocrKeys.has(item.key);
				const prevOcrUsed = (prev && prev.meta && prev.meta.ocrUsedKeys) || [];
				const ocrNew = hasOcr && prevOcrUsed.indexOf(item.key) === -1;
				const prevVer = prev && prev.versions ? prev.versions[item.key] : undefined;
				if (prev && canReuse && !ocrNew && (prevVer === undefined ? false : prevVer === ver) && prev.byItem.has(item.key)) {
					for (const idx of prev.byItem.get(item.key)) {
						const oc = prev.chunks[idx];
						chunks.push({
							cid: oc.cid, itemKey: oc.itemKey, itemID: oc.itemID,
							parentKey: oc.parentKey || '', libraryID: oc.libraryID, title: oc.title,
							retracted: !!oc.retracted, page: oc.page, pageSource: oc.pageSource,
							role: oc.role || ((oc.page === 1) ? 'front' : ''),
							// 兼容旧索引：字段缺失时从已有信息推导，避免增量后标记丢失
							orphan: (oc.orphan !== undefined) ? !!oc.orphan : !oc.parentKey,
							ocr: (oc.ocr !== undefined) ? !!oc.ocr : (oc.pageSource === 'ocr'),
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
						const embs = await embedSafe(cs.map(embedInputOf));
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
					if (!silent) ui('建立检索索引', done + '/' + ids.length + ' 附件，' + chunks.length + ' 块');
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
			// 重复正文去重：同一篇文献若在多个库、或同一库的多个副本都被建了块，只保留一份。
			// 判据 = 正文前 1200 字（去空白）指纹。
			// 保留顺序：① 库优先级（libraryPriority，默认 3>1>2）② 非孤立附件优先 ③ 先到先得。
			// v0.1.23 修复：①② 相同（常见于同库重复）时原先按"块数组先到"保留，会把孤儿副本留下、
			// 把有父条目的正规副本删掉——实测导致 2 条评估金标 itemKey 定位失败、orphan/parent 统计错乱。
			let crossLibDropped = 0;
			if (chunks.length && vecs.length === chunks.length) {
				const prio = String(getPref('libraryPriority', '3,1,2')).split(/[,，\s]+/).map(x => parseInt(x, 10)).filter(x => !isNaN(x));
				const rankOf = lib => { const i = prio.indexOf(lib); return i < 0 ? 999 : i; };
				const sigMap = new Map();   // sig -> {rank, orph, keep:[i], drop:[i]}
				for (let i = 0; i < chunks.length; i++) {
					const c = chunks[i];
					const txt = String(c.text || '').replace(/\s+/g, '').slice(0, 1200);
					if (!txt) continue;
					let h = 5381;
					for (let j = 0; j < txt.length; j++) h = ((h * 33) ^ txt.charCodeAt(j)) >>> 0;
					const key = h.toString(36) + '|' + txt.length;
					const r = rankOf(c.libraryID);
					const o = c.orphan ? 1 : 0;            // 0 = 有父条目（更优，优先保留）
					let rec = sigMap.get(key);
					if (!rec) { rec = { rank: r, orph: o, keep: [i], drop: [] }; sigMap.set(key, rec); }
					else if (r < rec.rank || (r === rec.rank && o < rec.orph)) {
						rec.drop.push(...rec.keep); rec.keep = [i]; rec.rank = r; rec.orph = o;
					}
					else rec.drop.push(i);
				}
				const dropSet = new Set();
				for (const rec of sigMap.values()) for (const d of rec.drop) dropSet.add(d);
				if (dropSet.size) {
					const keptChunks = [], keptVecs = [];
					for (let i = 0; i < chunks.length; i++) {
						if (dropSet.has(i)) continue;
						keptChunks.push(chunks[i]); keptVecs.push(vecs[i]);
					}
					crossLibDropped = dropSet.size;
					chunks.length = 0; chunks.push(...keptChunks);
					vecs.length = 0; vecs.push(...keptVecs);
					log('重复正文去重：丢弃 ' + crossLibDropped + ' 块，保留 ' + chunks.length + ' 块');
				}
			}
			// 跨库去重后必须重新打包向量（否则 chunks 与 vectors 顺序错位）
			if (crossLibDropped > 0 && vecs.length && vecs.length === chunks.length) {
				const packed2 = RetrievalCore.packVectors(vecs);
				vecData = packed2.data; dimOut = packed2.dim;
				log('去重后重新打包向量：' + chunks.length + ' 行');
			}

			// 从最终 chunks 重新统计（增量构建时单次计数会失真）
			let statOrphan = 0, statOcrChunks = 0, statRetracted = 0, statCache = 0;
			const statParents = new Set(), statPapers = new Set();
			for (const c of chunks) {
				if (c.orphan) statOrphan++;
				if (c.ocr || c.pageSource === 'ocr') statOcrChunks++;
				if (c.retracted) statRetracted = 1;
				if (c.pageSource === 'cache') statCache++;
				statParents.add(c.parentKey || c.itemKey);
				statPapers.add(c.itemKey);
			}
			ocrUsed = statOcrChunks > 0 ? Math.max(ocrUsed, new Set(chunks.filter(c => c.ocr || c.pageSource === 'ocr').map(c => c.itemKey)).size) : 0;
			cacheRecovered = Math.max(cacheRecovered, statCache > 0 ? new Set(chunks.filter(c => c.pageSource === 'cache').map(c => c.itemKey)).size : 0);
			retractedCount = statRetracted;
			await saveIndex(chunks, vecData, dimOut, {
				builtAt: new Date().toISOString(), libraries, model: getPref('model', 'bge-m3'),
				fingerprint, indexSchema: 2, crossLibDropped,
				orphanChunks: statOrphan, ocrChunks: statOcrChunks, cacheChunks: statCache,
				parents: statParents.size, papersById: statPapers.size,
				ollamaOnline: doEmbed, embedErrors, embedDim: dimOutProbe,
				cacheRecovered, retractedPapers: retractedCount, reused, itemVersions,
				ocrUsed, ocrUsedKeys, failedItems, pageGaps,
				sourceAttachments: used, failed, truncated, elapsedMs: Date.now() - t0
			});
			cached = null; bm25 = null; parentKeyCache.clear();
			const secs = ((Date.now() - t0) / 1000).toFixed(0);
			buildState = Object.assign({}, buildState, { running: false, phase: 'done', processed: done, chunks: chunks.length, failed, cacheRecovered, retractedCount, seconds: +secs, hasVectors: !!vecData });
			if (!silent) ui('建立检索索引', '完成：' + chunks.length + ' 块 / ' + used + ' 篇，用时 ' + secs + ' 秒' + (cacheRecovered ? '，缓存回填 ' + cacheRecovered : ''));
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
		// 篇级聚合（v0.1.26，T11 离线验证）：同一文献前两块 RRF 分数按 s1 + 0.4*s2 排序。
		// 动机：块级排序里「只有一块勉强命中」的文献会排在「两块互相印证」的文献前面；
		// 官方 75 条金标：Hit@1 78.67%→84.00%（+5.33pp）、R@5 不变、MRR 0.8446→0.8763，
		// 4 条查询升到 rank1，2 条变差（原本就在 40 名外，不影响 R@5）。
		{
			const W2 = 0.4;
			const tkCache = new Map();
			const agg = new Map();
			const tkOf = (ci) => {
				let tk = tkCache.get(ci);
				if (tk !== undefined) return tk;
				const c = idx.chunks[ci];
				if (!c) { tk = ''; }
				else {
					const isRet = c.retracted !== undefined ? !!c.retracted : isRetractedTitle(c.title);
					if (isRet && !includeRetracted) tk = '';
					else if (!useOrphans && c.orphan) tk = '';
					else tk = titleKeyOf(cleanTitle(c.title)) || resolveParentKey(c);
				}
				tkCache.set(ci, tk);
				return tk;
			};
			for (const [ci, base] of ranked) {
				const tk = tkOf(ci);
				if (!tk) continue;
				const a = agg.get(tk);
				if (!a) agg.set(tk, { s1: base, s2: -1 });
				else if (base > a.s1) { a.s2 = a.s1; a.s1 = base; }
				else if (base > a.s2) { a.s2 = base; }
			}
			const aggScore = (ci) => {
				const a = agg.get(tkOf(ci));
				if (!a) return -1;
				return a.s1 + (a.s2 > 0 ? W2 * a.s2 : 0);
			};
			ranked.sort((x, y) => {
				const sx = aggScore(x[0]), sy = aggScore(y[0]);
				if (sy !== sx) return sy - sx;
				return y[1] - x[1];
			});
		}

		const out = [];
		const perPaper = new Map();          // 按父条目去重（同一文献多个 PDF 副本）
		const seenText = new Set();          // 完全相同的片段只留一条
		let skippedRetracted = 0, skippedOrphans = 0, poorTitleSeen = 0;
		// 单轮输出，按融合排序依次取块（cap2/去重/摘要页先验不变）。
		// 历史坑（v0.1.25 修复）：旧实现是「两轮循环 + 第一轮跳过低信息量标题(titlePoor)」，
		// 但第一轮就把 kk 个名额填满 → 第二轮永远不执行 → 标题被判 titlePoor 的文献
		// （扫描件/翻译稿文件名，如 "AIC2017翻译"、"document"）在检索里完全不可见。
		// 实测 60/835 篇、4,023 块受影响；插件真实 R@5 从 90.67% 掉到 82.67%。
		// 现在不再排除，完全交给内容相关性排序（titlePoor 仅保留统计用途）。
		for (const [i] of ranked) {
			if (out.length >= kk) break;
			const c = idx.chunks[i];
			if (!c) continue;
			const isRet = c.retracted !== undefined ? !!c.retracted : isRetractedTitle(c.title);
			if (isRet && !includeRetracted) { skippedRetracted++; continue; }
			if (!useOrphans && c.orphan) { skippedOrphans++; continue; }
			const pk = resolveParentKey(c);
			const ctitle = cleanTitle(c.title);
			if (titlePoor(c.title)) poorTitleSeen++;
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
					ok: true, plugin: 'retrieval-index', version: '0.1.27',
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

		// 管理接口：设置分类、追加标签（仅整理元数据，不改条目内容）
		Zotero.Server.Endpoints['/retrieval/admin/organize'] = function () {};
		Zotero.Server.Endpoints['/retrieval/admin/organize'].prototype = {
			supportedMethods: ['POST'],
			supportedDataTypes: ['application/json'],
			init: async function (options) {
				const out = { ok: true, results: [] };
				try {
					const body = options.data || {};
					const items = body.items || [];
					if (!items.length) return [400, 'application/json', JSON.stringify({ ok: false, error: '需要 items' })];
					for (const spec of items) {
						const r = { key: spec.key, ok: false };
						try {
							const libID = parseInt(spec.libraryID, 10) || 3;
							const item = await Zotero.Items.getByLibraryAndKeyAsync(libID, spec.key);
							if (!item) throw new Error('找不到条目 ' + spec.key);
							// 分类：collection key → collection ID
							if (Array.isArray(spec.collections)) {
								const ids = [];
								for (const ck of spec.collections) {
									let col = null;
									try { col = await Zotero.Collections.getByLibraryAndKeyAsync(libID, ck); } catch (e) {}
									if (col) ids.push(col.id);
									else log('分类 key 无效：' + ck);
								}
								if (ids.length) { item.setCollections(ids); r.collections = ids.length; }
							}
							// 标签：追加（不覆盖已有）
							if (Array.isArray(spec.addTags)) {
								let n = 0;
								for (const tg of spec.addTags) {
									if (tg) { item.addTag(String(tg)); n++; }
								}
								r.addedTags = n;
							}
							await item.saveTx();
							r.ok = true;
							r.title = item.getField('title');
							log('已整理 ' + spec.key + '：分类 ' + (r.collections || 0) + ' / 标签 ' + (r.addedTags || 0));
						} catch (e) {
							r.error = String((e && e.message) || e);
							out.ok = false;
						}
						out.results.push(r);
					}
					return [200, 'application/json', JSON.stringify(out)];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ ok: false, error: String((e && e.message) || e) })];
				}
			}
		};

		// 管理接口：把现有附件导入指定库（新建父条目 + 复制 PDF），仅新增
		Zotero.Server.Endpoints['/retrieval/admin/import'] = function () {};
		Zotero.Server.Endpoints['/retrieval/admin/import'].prototype = {
			supportedMethods: ['POST'],
			supportedDataTypes: ['application/json'],
			init: async function (options) {
				const out = { ok: true, results: [] };
				try {
					const body = options.data || {};
					const libID = parseInt(body.libraryID, 10);
					const files = body.files || [];
					if (!libID || !files.length) {
						return [400, 'application/json', JSON.stringify({ ok: false, error: '需要 libraryID 与 files' })];
					}
					for (const f of files) {
						const r = { sourceItemKey: f.sourceItemKey, ok: false };
						try {
							// 1) 定位源附件（跨库按 key 找）
							let att = null;
							for (const lib of Zotero.Libraries.getAll()) {
								try {
									const it = await Zotero.Items.getByLibraryAndKeyAsync(lib.libraryID, f.sourceItemKey);
									if (it) { att = it; break; }
								} catch (e) {}
							}
							if (!att) throw new Error('找不到源附件 ' + f.sourceItemKey);
							const path = await att.getFilePathAsync();
							if (!path) throw new Error('源附件无文件');
							// 2) 新建父条目
							const md = f.metadata || {};
							const item = new Zotero.Item(md.itemType || 'journalArticle');
							item.libraryID = libID;
							for (const k of Object.keys(md)) {
								if (k === 'itemType') continue;
								try { item.setField(k, md[k]); } catch (e) {}
							}
							if (Array.isArray(f.creators) && f.creators.length) {
								item.setCreators(f.creators.map(c => ({
									firstName: c.firstName || '', lastName: c.lastName || '',
									creatorType: c.creatorType || 'author', fieldMode: 0
								})));
							}
							const itemID = await item.saveTx();
							r.parentItemID = itemID;
							r.parentKey = item.key;
							// 3) 复制 PDF 为该条目的子附件
							const attID = await Zotero.Attachments.importFromFile({
								file: path, parentItemID: itemID, libraryID: libID
							});
							r.attachmentID = attID;
							try {
								const na = Zotero.Items.get(attID);
								r.attachmentKey = na ? na.key : null;
							} catch (e) {}
							r.ok = true;
							log('已导入库 ' + libID + '：' + (md.title || '') + ' → ' + r.parentKey);
						} catch (e) {
							r.error = String((e && e.message) || e);
							out.ok = false;
						}
						out.results.push(r);
					}
					return [200, 'application/json', JSON.stringify(out)];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ ok: false, error: String((e && e.message) || e) })];
				}
			}
		};

		// 管理接口：重复附件清理（默认 dry-run；apply 需 confirm 口令；只删附件、不删父条目、不删索引内条目）
		// body: { dryRun?: true|false, confirm?: "DELETE-DUPLICATES", items: [{libraryID, key}, ...] }
		Zotero.Server.Endpoints['/retrieval/admin/dedup'] = function () {};
		Zotero.Server.Endpoints['/retrieval/admin/dedup'].prototype = {
			supportedMethods: ['POST'],
			supportedDataTypes: ['application/json'],
			init: async function (options) {
				try {
					const body = options.data || {};
					const items = Array.isArray(body.items) ? body.items : [];
					const apply = body.dryRun === false;
					if (!items.length) {
						return [400, 'application/json', JSON.stringify({ ok: false, error: '需要 items:[{libraryID,key}]' })];
					}
					if (items.length > 200) {
						return [400, 'application/json', JSON.stringify({ ok: false, error: '单次最多 200 条，请分批' })];
					}
					if (apply && body.confirm !== 'DELETE-DUPLICATES') {
						return [400, 'application/json', JSON.stringify({
							ok: false,
							error: 'apply 需要 confirm:"DELETE-DUPLICATES"（请先 dryRun 核对清单）'
						})];
					}
					// 取当前索引的 itemKey 集合：索引内的附件一律拒绝删除，保护检索内容
					let indexed = new Set();
					try {
						await loadIndex();
						if (cached && cached.chunks) for (const c of cached.chunks) indexed.add(c.itemKey);
					} catch (e) { log('dedup：读取索引失败，将拒绝 apply：' + e); }
					const out = { ok: true, dryRun: !apply, results: [], backupPath: null, trashed: 0 };
					const backup = [];
					for (const spec of items) {
						const r = { key: spec.key, libraryID: parseInt(spec.libraryID, 10) || 0, action: 'skip', reason: '' };
						try {
							const item = await Zotero.Items.getByLibraryAndKeyAsync(r.libraryID, spec.key);
							if (!item) { r.reason = '找不到条目'; out.results.push(r); continue; }
							r.title = item.getField('title');
							r.itemType = item.itemType;
							r.parentKey = item.parentKey || '';
							r.deleted = !!item.deleted;
							r.inIndex = indexed.has(spec.key);
							if (item.deleted) { r.reason = '已在回收站，跳过'; out.results.push(r); continue; }
							if (item.itemType !== 'attachment') { r.reason = '只允许删除附件条目（拒绝删父条目）'; out.results.push(r); continue; }
							if (r.inIndex) { r.reason = '该附件在当前索引中，拒绝删除（保护检索内容）'; out.results.push(r); continue; }
							if (!indexed.size && apply) { r.reason = '索引未加载，无法校验 inIndex，拒绝 apply'; out.results.push(r); continue; }
							let snap = null;
							try { snap = item.toJSON(); } catch (e) { snap = { key: item.key, libraryID: item.libraryID, title: r.title, parentKey: r.parentKey }; }
							backup.push({ key: spec.key, libraryID: r.libraryID, version: item.version, snapshot: snap });
							r.action = apply ? 'trashed' : 'would-trash';
						} catch (e) {
							r.reason = String((e && e.message) || e);
						}
						out.results.push(r);
					}
					if (apply) {
						// 先落备份，再执行（任何一条失败都不影响已备份数据）
						try {
							out.backupPath = idxPath('admin-dedup-backup-' + Date.now() + '.json');
							await Zotero.File.putContentsAsync(out.backupPath, JSON.stringify({
								createdAt: new Date().toISOString(),
								confirm: body.confirm,
								count: backup.length,
								items: backup
							}, null, 1));
						} catch (e) {
							return [500, 'application/json', JSON.stringify({ ok: false, error: '备份写入失败，已中止（未删除任何条目）：' + (e.message || e), results: out.results })];
						}
						for (const r of out.results) {
							if (r.action !== 'trashed') continue;
							try {
								const item = await Zotero.Items.getByLibraryAndKeyAsync(r.libraryID, r.key);
								if (!item) { r.action = 'skip'; r.reason = '执行时找不到条目'; continue; }
								item.deleted = true;
								await item.saveTx();
								out.trashed++;
							} catch (e) {
								r.action = 'error';
								r.reason = String((e && e.message) || e);
								out.ok = false;
							}
						}
					}
					log('dedup：' + (apply ? '已移入回收站 ' + out.trashed + ' 条' : 'dry-run ' + out.results.length + ' 条') + '，备份 ' + (out.backupPath || '（dry-run 不写）'));
					return [200, 'application/json', JSON.stringify(out)];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ ok: false, error: String((e && e.message) || e) })];
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

		// Agent 使用协议：让 agent 一条 curl 自举用法。
		// 正文由 build.py 从 docs/agent-usage-protocol.md 生成到 content/protocol-text.js（勿手改）。
		Zotero.Server.Endpoints['/retrieval/protocol'] = function () {};
		Zotero.Server.Endpoints['/retrieval/protocol'].prototype = {
			supportedMethods: ['GET'],
			init: function (o) {
				let md = '';
				try { md = (typeof RETRIEVAL_PROTOCOL_MD === 'string') ? RETRIEVAL_PROTOCOL_MD : ''; } catch (e) {}
				if (!md) {
					return [503, 'application/json', JSON.stringify({
						ok: false,
						error: '协议文本未打包：请在源码目录运行 python build.py 后重新安装',
						docPath: 'docs/agent-usage-protocol.md'
					})];
				}
				const fmt = (o && o.searchParams && o.searchParams.get('format')) || 'md';
				if (fmt === 'json') {
					return [200, 'application/json', JSON.stringify({ ok: true, version: '0.1.23', format: 'markdown', chars: md.length, protocol: md })];
				}
				return [200, 'text/markdown; charset=utf-8', md];
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
		for (const p of ['/retrieval/ping', '/retrieval/stats', '/retrieval/search', '/retrieval/build', '/retrieval/ollama', '/retrieval/protocol', '/retrieval/ocr-candidates', '/retrieval/admin/import', '/retrieval/admin/organize', '/retrieval/admin/dedup']) {
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
			// 自动增量：启动后延迟执行；可配置周期
			try {
				const autoOn = String(getPref('autoIncremental', 1)) !== '0' && !!getPref('autoIncremental', 1);
				if (autoOn) {
					const delayMs = parseInt(getPref('autoIncrementalDelaySec', 90), 10) * 1000;
					const everyMin = parseInt(getPref('autoIncrementalMinutes', 0), 10);
					(async () => {
						await Zotero.Promise.delay(Math.max(10000, delayMs));
						try {
							const r = await build({ incremental: 1, silent: 1 });
							log('自动增量完成：' + (r && r.noChange ? '无变更' : (r.chunks + ' 块')));
						} catch (e) { log('自动增量失败：' + (e && e.message || e)); }
						if (everyMin > 0) {
							while (true) {
								await Zotero.Promise.delay(everyMin * 60000);
								try { await build({ incremental: 1, silent: 1 }); }
								catch (e) { log('周期增量失败：' + (e && e.message || e)); }
							}
						}
					})();
					log('已启用自动增量（延迟 ' + (delayMs / 1000) + ' 秒' + (everyMin ? '，每 ' + everyMin + ' 分钟' : '') + '）');
				}
			} catch (e) { log('自动增量注册失败：' + e); }
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
