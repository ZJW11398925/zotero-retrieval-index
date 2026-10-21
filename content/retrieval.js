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
			let bin = '';
			const step = 0x8000;
			for (let i = 0; i < q.length; i += step) {
				bin += String.fromCharCode.apply(null, q.subarray(i, i + step));
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
		cached = { chunks, matrix, dim, meta };
		log('索引已加载：' + chunks.length + ' 块' + (matrix ? '（含向量）' : '（无向量）'));
		return cached;
	}

	function getBm25() {
		if (bm25 || !cached) return bm25;
		const docs = cached.chunks.map(c => RetrievalCore.tokenize(c.text));
		bm25 = RetrievalCore.buildBm25(docs);
		return bm25;
	}

	// ---------------- Ollama ----------------
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

	async function embedOne(text) {
		const ac = new AbortController();
		const timer = setTimeout(() => ac.abort(), 20000);
		try {
			const res = await fetch(ollamaBase() + '/api/embed', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ model: getPref('model', 'bge-m3'), input: [text] }),
				signal: ac.signal, redirect: 'error'
			});
			if (!res.ok) throw new Error('HTTP ' + res.status);
			const j = await res.json();
			return j.embeddings[0];
		} finally { clearTimeout(timer); }
	}

	async function ollamaAvailable() {
		try {
			const ac = new AbortController();
			const t = setTimeout(() => ac.abort(), 4000);
			const res = await fetch(ollamaBase() + '/api/tags', { signal: ac.signal });
			clearTimeout(t);
			return res.ok;
		} catch (e) { return false; }
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
	async function build({ limit = 0 } = {}) {
		if (building) throw new Error('已有建索引任务在运行');
		building = true;
		cancelRequested = false;
		buildState = { running: true, phase: 'scanning', processed: 0, total: 0, chunks: 0, failed: 0, startedAt: Date.now(), error: null };
		const t0 = Date.now();
		try {
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
			for (const rec of ids) {
				const id = rec.id;
				if (cancelRequested) { log('用户取消'); break; }
				const item = Zotero.Items.get(id);
				if (!item || !item.isPDFAttachment() || !item.parentItemID) { done++; continue; }
				used++;
				let pages = [];
				try {
					const res = await Zotero.PDFWorker.getFullText(id, maxPages);
					if (!res || !res.text) { failed++; done++; continue; }
					if (res.totalPages > maxPages) truncated++;
					pages = String(res.text).split('\f');
				} catch (e) { failed++; done++; continue; }
				let title = '';
				try {
					const parent = Zotero.Items.get(item.parentItemID);
					title = parent ? (parent.getField('title') || '') : '';
				} catch (e) {}
				let cs = [];
				try { cs = RetrievalCore.chunkPages(pages, { chunkChars, overlapChars: 80, minChunk: 120 }); }
				catch (e) { failed++; done++; continue; }
				for (let i = 0; i < cs.length; i++) {
					chunks.push({
						cid: item.key + '#' + cs[i].page + '#' + i, itemKey: item.key, itemID: id,
						libraryID: rec.libraryID, title, page: cs[i].page, text: cs[i].text
					});
				}
				if (doEmbed && cs.length) {
					try {
						const embs = await embedBatch(cs.map(c => c.text));
						if (embs[0]) dimOutProbe = embs[0].length;
						for (const e of embs) vecs.push(e);
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
			}
			await saveIndex(chunks, vecData, dimOut, {
				builtAt: new Date().toISOString(), libraries, model: getPref('model', 'bge-m3'),
				ollamaOnline: doEmbed, embedErrors, embedDim: dimOutProbe,
				sourceAttachments: used, failed, truncated, elapsedMs: Date.now() - t0
			});
			cached = null; bm25 = null;
			const secs = ((Date.now() - t0) / 1000).toFixed(0);
			buildState = Object.assign({}, buildState, { running: false, phase: 'done', processed: done, chunks: chunks.length, failed, seconds: +secs, hasVectors: !!vecData });
			ui('建立检索索引', '完成：' + chunks.length + ' 块 / ' + used + ' 篇，用时 ' + secs + ' 秒');
			uiClose();
			return { chunks: chunks.length, attachments: used, failed, truncated, seconds: +secs };
		} catch (e) {
			buildState = Object.assign({}, buildState, { running: false, phase: 'error', error: String((e && e.message) || e) });
			throw e;
		} finally {
			building = false;
			uiClose();
		}
	}

	// ---------------- 检索 ----------------
	async function search({ q, k = 8, mode = 'hybrid' } = {}) {
		if (!q || !String(q).trim()) throw new Error('缺少查询词 q');
		const idx = await loadIndex();
		if (!idx || !idx.chunks.length) throw new Error('尚未建立索引');
		const bm = getBm25();
		const kk = Math.max(1, Math.min(50, parseInt(k, 10) || 8));
		const rk = [];
		rk.push(RetrievalCore.topN(RetrievalCore.bm25Score(RetrievalCore.tokenize(q), bm), 200));
		if (mode !== 'bm25' && idx.matrix && idx.dim) {
			try {
				const dn = RetrievalCore.denseScores(await embedOne(String(q)), idx.matrix, idx.dim);
				rk.push(RetrievalCore.topN(dn, 200));
			} catch (e) { log('查询向量失败：' + e); }
		}
		const order = rk.length > 1 ? RetrievalCore.rrf(rk) : rk[0];
		const out = [];
		const perPaper = new Map();
		for (const i of order) {
			if (out.length >= kk) break;
			const c = idx.chunks[i];
			if (!c) continue;
			const n = perPaper.get(c.itemKey) || 0;
			if (n >= 3) continue;
			perPaper.set(c.itemKey, n + 1);
			out.push({ itemKey: c.itemKey, title: c.title, page: c.page, text: c.text.length > 1200 ? c.text.slice(0, 1200) + '…' : c.text });
		}
		return out;
	}

	function stats() {
		const out = { build: buildState, dataDir, startupError };
		if (!cached) return Object.assign({ loaded: false }, out);
		return Object.assign({
			loaded: true, chunks: cached.chunks.length,
			papers: new Set(cached.chunks.map(c => c.itemKey)).size,
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
					ok: true, plugin: 'retrieval-index', version: '0.1.3',
					dataDir, hasDataDir: !!dataDir, startupError, ts: new Date().toISOString()
				})];
			}
		};

		Zotero.Server.Endpoints['/retrieval/ollama'] = function () {};
		Zotero.Server.Endpoints['/retrieval/ollama'].prototype = {
			supportedMethods: ['GET'],
			init: async function (options) {
				const url = ollamaBase() + '/api/tags';
				try {
					const ac = new AbortController();
					const timer = setTimeout(() => ac.abort(), 8000);
					const res = await fetch(url, { signal: ac.signal });
					clearTimeout(timer);
					let models = [];
					try { const j = await res.json(); models = (j.models || []).map(m => m.name); } catch (e) {}
					return [200, 'application/json', JSON.stringify({ ok: res.ok, url, models })];
				} catch (e) {
					return [200, 'application/json', JSON.stringify({ ok: false, url, error: String((e && e.message) || e) })];
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
						mode: o.searchParams.get('mode') || 'hybrid'
					});
					return [200, 'application/json', JSON.stringify(r)];
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
					build({ limit }).catch(e => log('建索引失败：' + e));
					return [200, 'application/json', JSON.stringify({ started: true, limit })];
				} catch (e) {
					return [500, 'application/json', JSON.stringify({ error: String((e && e.message) || e) })];
				}
			}
		};
		log('接口已注册');
	}

	function unregisterEndpoints() {
		for (const p of ['/retrieval/ping', '/retrieval/stats', '/retrieval/search', '/retrieval/build', '/retrieval/ollama']) {
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
			mk('test', '检索自检（示例查询）', async () => {
				try {
					const r = await search({ q: '儿童色彩偏好', k: 3 });
					Services.prompt.alert(null, '检索自检', r.map((x, i) => (i + 1) + '. [' + x.page + '页] ' + (x.title || '').slice(0, 30) + '\n   ' + x.text.slice(0, 60) + '…').join('\n\n') || '无结果');
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
