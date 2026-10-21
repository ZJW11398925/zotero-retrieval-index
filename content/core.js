/**
 * 纯逻辑核心：不依赖 Zotero 任何对象，可在 Node 里单独测试。
 * 分块 / 分词 / BM25 / RRF / 向量打包。
 */
var RetrievalCore = (function () {
	'use strict';

	const CJK = /[\u4e00-\u9fff]+/g;
	const WORD = /[A-Za-z][A-Za-z0-9_\-]{1,}/g;
	const HAN_ONLY = /^[\u4e00-\u9fff]+$/;

	// 用 ICU 分词（Gecko/Node 均内置），避免自造中文切分
	let SEG = null;
	try { SEG = new Intl.Segmenter('zh', { granularity: 'word' }); } catch (e) { SEG = null; }

	const STOP = new Set(('的 了 和 与 及 或 在 是 为 对 以 于 中 被 将 等 这 那 其 之 而 有 并 也 就 要 会 能 可 一个 我们 他们 进行 通过 本文 本文 研究 结果 表明 分析 问题 方法 以及 因此 但是 如果 虽然 所以 由于 根据 其中 一 二 三 四 五 六 七 八 九 十').split(' '));

	/** 分词：ICU 词切分 + 长词内部二元补充；无 ICU 时退化为二元字符。 */
	function tokenize(text) {
		const out = [];
		if (!text) return out;
		if (SEG) {
			for (const seg of SEG.segment(text)) {
				if (!seg.isWordLike) continue;
				let w = String(seg.segment).toLowerCase();
				if (w.length < 2) continue;
				if (STOP.has(w)) continue;
				out.push(w);
				// 长中文词补内部二元，提升部分匹配（如 中学生 → 中学/学生）
				if (w.length >= 3 && HAN_ONLY.test(w)) {
					for (let i = 0; i < w.length - 1; i++) out.push(w.substr(i, 2));
				}
			}
			return out;
		}
		const cjk = text.match(CJK);
		if (cjk) {
			for (const run of cjk) {
				if (run.length === 1) { out.push(run); continue; }
				for (let i = 0; i < run.length - 1; i++) out.push(run.substr(i, 2));
			}
		}
		const words = text.toLowerCase().match(WORD);
		if (words) out.push(...words);
		return out;
	}

	/**
	 * 按页切块，不跨页——页码才能如实。
	 * @param {string[]} pages 第 0 项对应第 1 页
	 */
	function chunkPages(pages, opts) {
		const cfg = Object.assign({ chunkChars: 520, overlapChars: 80, minChunk: 120 }, opts || {});
		const chunks = [];
		for (let pi = 0; pi < pages.length; pi++) {
			const raw = String(pages[pi] || '').replace(/\u0000/g, ' ');
			const paras = raw.split(/\n+/).map(s => s.trim()).filter(Boolean);
			let buf = [], size = 0;
			const flush = () => {
				const t = buf.join('\n');
				if (t.length >= cfg.minChunk) chunks.push({ page: pi + 1, text: t });
				// 尾部重叠
				const tail = []; let tl = 0;
				for (let i = buf.length - 1; i >= 0; i--) {
					if (tl + buf[i].length > cfg.overlapChars) break;
					tail.unshift(buf[i]); tl += buf[i].length;
				}
				buf = tail; size = tl;
			};
			for (const p of paras) {
				if (size + p.length > cfg.chunkChars && buf.length) flush();
				buf.push(p); size += p.length;
			}
			if (buf.length) {
				const t = buf.join('\n');
				if (t.length >= cfg.minChunk) chunks.push({ page: pi + 1, text: t });
			}
		}
		return chunks;
	}

	const K1 = 1.5, B = 0.75;

	/** 建倒排索引：token -> [{d, tf}] */
	function buildBm25(docs) {
		const postings = new Map();
		const dl = new Float64Array(docs.length);
		for (let d = 0; d < docs.length; d++) {
			const tf = new Map();
			const toks = docs[d];
			for (let i = 0; i < toks.length; i++) tf.set(toks[i], (tf.get(toks[i]) || 0) + 1);
			dl[d] = toks.length;
			for (const [t, n] of tf) {
				let arr = postings.get(t);
				if (!arr) { arr = []; postings.set(t, arr); }
				arr.push(d, n);
			}
		}
		let total = 0;
		for (let d = 0; d < docs.length; d++) total += dl[d];
		// idf 用文档频次
		const df = new Map();
		for (const [t, arr] of postings) df.set(t, arr.length / 2);
		const N = docs.length || 1;
		return { postings, df, dl, avgdl: docs.length ? total / docs.length : 0, N, K1, B };
	}

	/** 返回 Float32Array 分数 */
	function bm25Score(qTokens, idx, out) {
		const sc = out && out.length === idx.N ? out : new Float32Array(idx.N);
		if (out) sc.fill(0);
		const seen = new Set();
		const { K1, B, dl, avgdl, N, df, postings } = idx;
		for (const t of qTokens) {
			if (seen.has(t)) continue;
			seen.add(t);
			const arr = postings.get(t);
			if (!arr) continue;
			const d = df.get(t);
			const idf = Math.log(1 + (N - d + 0.5) / (d + 0.5));
			for (let i = 0; i < arr.length; i += 2) {
				const di = arr[i], tf = arr[i + 1];
				sc[di] += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl[di] / avgdl));
			}
		}
		return sc;
	}

	/** 取 top-n 下标 */
	function topN(scores, n) {
		const idx = [];
		for (let i = 0; i < scores.length; i++) if (scores[i] > 0) idx.push(i);
		idx.sort((a, b) => scores[b] - scores[a]);
		return idx.slice(0, n);
	}

	/** 余弦（向量已归一化时即点积） */
	function denseScores(queryVec, matrix, dim) {
		const n = matrix.length / dim;
		const sc = new Float32Array(n);
		for (let i = 0; i < n; i++) {
			let s = 0;
			const base = i * dim;
			for (let j = 0; j < dim; j++) s += matrix[base + j] * queryVec[j];
			sc[i] = s;
		}
		return sc;
	}

	function rrf(rankLists, k) {
		k = k || 60;
		const sc = new Map();
		for (const rl of rankLists) {
			for (let i = 0; i < rl.length; i++) {
				sc.set(rl[i], (sc.get(rl[i]) || 0) + 1 / (k + i + 1));
			}
		}
		return [...sc.entries()].sort((a, b) => b[1] - a[1]).map(e => e[0]);
	}

	/** 把 [[...dim], ...] 打包成 Float32Array 并用 L2 归一化 */
	function packVectors(vecs) {
		if (!vecs.length) return { data: new Float32Array(0), dim: 0 };
		const dim = vecs[0].length;
		const data = new Float32Array(vecs.length * dim);
		for (let i = 0; i < vecs.length; i++) {
			let norm = 0;
			for (let j = 0; j < dim; j++) norm += vecs[i][j] * vecs[i][j];
			norm = Math.sqrt(norm) || 1;
			for (let j = 0; j < dim; j++) data[i * dim + j] = vecs[i][j] / norm;
		}
		return { data, dim };
	}

	return { tokenize, chunkPages, buildBm25, bm25Score, topN, denseScores, rrf, packVectors };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = RetrievalCore;
