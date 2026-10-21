// 用真实逐页语料验证 core.js：分词、分块、BM25 召回
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const C = require('../content/core.js');

const DIR = 'D:/zotero/eval_pages';
const chunks = fs.readFileSync(`${DIR}/chunks.jsonl`, 'utf8').trim().split('\n').map(JSON.parse);
const Q = JSON.parse(fs.readFileSync(`${DIR}/queries.json`, 'utf8'));
console.log(`语料 ${chunks.length} 块 / ${new Set(chunks.map(c => c.pid)).size} 篇`);

// 1) 分词抽样
console.log('\n=== 分词抽样 ===');
for (const s of ['校园空间色彩与儿童情绪反应之间的关联', 'Computational color preference research']) {
	console.log(`  ${s}\n    → ${C.tokenize(s).slice(0, 14).join(' ')}`);
}

// 2) 分块函数自检
console.log('\n=== chunkPages 自检 ===');
const pages = ['短页内容不足以下块', '第一段内容。\n第二段内容进一步展开说明研究的方法与结果。'.repeat(20)];
const cs = C.chunkPages(pages, { chunkChars: 200, minChunk: 80 });
console.log(`  2 页 → ${cs.length} 块，页码分布 ${[...new Set(cs.map(c => c.page))].join(',')}（应为 1,2，不跨页）`);

// 3) 建 BM25 索引
const t0 = Date.now();
const docs = chunks.map(c => C.tokenize(c.text));
const idx = C.buildBm25(docs);
console.log(`\nBM25 索引: ${idx.N} 文档 / ${idx.postings.size} 词 / ${Date.now() - t0} ms`);

const pids = chunks.map(c => c.pid);
const present = new Set(pids);
// 只保留金标文献确实在语料内的查询
for (const k of ['natural', 'keyword']) {
	if (Q[k]) {
		const b = Q[k].length;
		Q[k] = Q[k].filter(q => present.has(q.gt_pid));
		if (b !== Q[k].length) console.log(`  [${k}] 剔除金标缺失 ${b - Q[k].length} 条，剩 ${Q[k].length}`);
	}
}

function paperTop(sc, k) {
	const order = C.topN(sc, k * 4);
	const seen = [];
	for (const i of order) { if (!seen.includes(pids[i])) seen.push(pids[i]); if (seen.length >= k) break; }
	return seen;
}

function evalSet(name, list) {
	if (!list || !list.length) return;
	let ok5 = 0, ok1 = 0, n = 0;
	const detail = [];
	for (const q of list) {
		const sc = C.bm25Score(C.tokenize(q.q), idx);
		const top = paperTop(sc, 5);
		const rank = top.indexOf(q.gt_pid);
		if (rank >= 0) ok5++;
		if (rank === 0) ok1++;
		n++;
		detail.push({ q: q.q, rank: rank < 0 ? null : rank + 1 });
	}
	console.log(`\n=== ${name}（${n} 条）===`);
	console.log(`  篇级 Recall@5: ${(100 * ok5 / n).toFixed(1)}%   Hit@1: ${(100 * ok1 / n).toFixed(1)}%`);
	for (const d of detail.filter(x => x.rank === null).slice(0, 4)) {
		console.log(`    未命中: ${d.q}`);
	}
}

evalSet('自然语言问句（纯二元字符分词）', Q.natural);
evalSet('关键词查询（纯二元字符分词）', Q.keyword);

// 4) RRF 自检
const a = [3, 1, 2], b = [1, 4, 3];
console.log(`\nRRF 自检: ${JSON.stringify(C.rrf([a, b]))}（应把两路都靠前的排前面）`);
