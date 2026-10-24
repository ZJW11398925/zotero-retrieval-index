# -*- coding: utf-8 -*-
"""从 Zotero PDF 中抽取表格（PyMuPDF find_tables + 词级坐标重建单元格）

输出 sidecar：<out-dir>/<itemKey>.json
  {"itemKey","title","source","tables":[{"page":N,"caption":"...","markdown":"| a | b |\\n|---|---|\\n| .. |","nrows":R,"ncols":C}]}

用法：
  python tools/extract_tables.py --all --workers 4
  python tools/extract_tables.py --keys keys.txt --out-dir <dir>
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import pymupdf

sys.stdout.reconfigure(encoding='utf-8')
STORAGE = os.path.expanduser(r'~\Zotero\storage')
IDX = os.path.join(os.environ['APPDATA'], 'Zotero', 'Zotero', 'Profiles', 'default', 'retrieval-index')
DEFAULT_OUT = os.path.join(IDX, 'tables')
CAP_RE = re.compile(r'(表\s*\d+|Table\s*\d+|续表)')


def clean_cell(c):
    s = re.sub(r'[ \t\r\n]+', ' ', str(c or '')).strip()
    return s.replace('|', '/')


def cell_text(words, rect):
    x0, y0, x1, y1 = rect
    inside = []
    for w in words:
        cx = (w[0] + w[2]) / 2
        cy = (w[1] + w[3]) / 2
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            inside.append(w)
    if not inside:
        return ''
    inside.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines, cur, cur_y = [], [], None
    for w in inside:
        yc = (w[1] + w[3]) / 2
        h = w[3] - w[1]
        if cur_y is None or abs(yc - cur_y) <= max(2.0, h * 0.5):
            cur.append(w)
            cur_y = yc if cur_y is None else (cur_y * (len(cur) - 1) + yc) / len(cur)
        else:
            lines.append(cur)
            cur = [w]
            cur_y = yc
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w[0])
        out.append(''.join(w[4] for w in ln))
    return ' '.join(out)


def find_caption(page, top_y):
    """在表格上方区域找最近的“表 N”标题行"""
    try:
        clip = pymupdf.Rect(0, max(0, top_y - 110), page.rect.x1, top_y)
        txt = page.get_text('text', clip=clip)
    except Exception:
        return ''
    lines = [x.strip() for x in txt.split('\n') if x.strip()]
    for ln in reversed(lines):
        if CAP_RE.search(ln):
            return ln[:80]
    return ''


def page_likely_table(page):
    """页级预过滤：只对可能含表格的页做 find_tables（大 PDF 全页跑太慢）"""
    try:
        txt = page.get_text('text') or ''
    except Exception:
        return False
    if len(txt.strip()) < 80:
        return False
    if CAP_RE.search(txt):
        return True
    try:
        n_draw = len(page.get_drawings())
    except Exception:
        n_draw = 0
    if n_draw >= 12:
        return True
    return False


def extract_pdf(path):
    tables = []
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        return tables, 'open-fail:%s' % e
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            if not page_likely_table(page):
                continue
            try:
                found = page.find_tables(strategy='text')
            except Exception:
                continue
            if not found.tables:
                continue
            words = page.get_text('words')
            for t in found.tables:
                try:
                    nrows, ncols = t.row_count, t.col_count
                    cells = t.cells
                except Exception:
                    continue
                if nrows < 2 or ncols < 2:
                    continue
                grid = [[''] * ncols for _ in range(nrows)]
                for idx, rect in enumerate(cells):
                    r, c = divmod(idx, ncols)
                    if r < nrows and c < ncols:
                        grid[r][c] = clean_cell(cell_text(words, rect))
                # 去掉全空列/行
                keep_cols = [j for j in range(ncols) if sum(1 for r in grid if r[j]) >= max(2, nrows // 3)]
                if len(keep_cols) < 2:
                    continue
                grid = [[row[j] for j in keep_cols] for row in grid]
                grid = [r for r in grid if any(r)]
                if len(grid) < 2:
                    continue
                caption = find_caption(page, t.bbox[1])
                header, body = grid[0], grid[1:]
                md = '| ' + ' | '.join(header) + ' |\n| ' + ' | '.join(['---'] * len(header)) + ' |\n'
                md += '\n'.join('| ' + ' | '.join(r) + ' |' for r in body)
                tables.append({'page': pno + 1, 'caption': caption, 'markdown': md,
                               'nrows': len(body), 'ncols': len(header)})
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return tables, ''


def load_index_keys():
    chunks = json.load(open(os.path.join(IDX, 'chunks.json'), encoding='utf-8'))
    seen, order = set(), []
    for c in chunks:
        k = c.get('itemKey')
        if k and k not in seen:
            seen.add(k)
            order.append({'itemKey': k, 'title': c.get('title') or ''})
    return order


def pdf_of(item_key):
    hits = glob.glob(os.path.join(STORAGE, item_key, '*.pdf'))
    if not hits:
        hits = glob.glob(os.path.join(STORAGE, item_key, '**', '*.pdf'), recursive=True)
    hits.sort(key=lambda p: -os.path.getsize(p) if os.path.exists(p) else 0)
    return hits[0] if hits else None


def work(item):
    p = pdf_of(item['itemKey'])
    if not p:
        return None
    try:
        tables, err = extract_pdf(p)
    except Exception:
        return None
    if not tables:
        return None
    # 只记录文件名，不写本机绝对路径（分发包隐私要求）
    return {'itemKey': item['itemKey'], 'title': item['title'],
            'sourceFile': os.path.basename(str(p).replace('\\', '/')),
            'extractedAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'tables': tables}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pilot', type=int, default=0)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--keys', default=None)
    ap.add_argument('--out-dir', default=DEFAULT_OUT)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    titles = {c['itemKey']: c['title'] for c in load_index_keys()}
    if args.keys:
        keys = [x.strip() for x in open(args.keys, encoding='utf-8-sig') if x.strip()]
        items = [{'itemKey': k, 'title': titles.get(k, '')} for k in keys]
    else:
        items = load_index_keys()
        if args.pilot:
            items = items[:args.pilot]
    if args.limit:
        items = items[:args.limit]

    t0 = time.time()
    stats = {'items': 0, 'with_pdf': 0, 'tables': 0, 'items_with_table': 0, 'fails': 0}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(work, it): it for it in items}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                res = f.result()
            except Exception:
                stats['fails'] += 1
                continue
            if res is None:
                continue
            stats['items_with_table'] += 1
            stats['tables'] += len(res['tables'])
            json.dump(res, open(os.path.join(args.out_dir, res['itemKey'] + '.json'), 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            if done % 100 == 0:
                print('  %d/%d processed, items-with-table=%d, tables=%d, %.0fs' % (
                    done, len(items), stats['items_with_table'], stats['tables'], time.time() - t0), flush=True)
    json.dump(stats, open(os.path.join(args.out_dir, '_stats.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    # 生成清单（插件 build 读取 _manifest.json 判断哪些条目有表格旁路）
    keys = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(args.out_dir, '*.json'))
                  if not os.path.basename(f).startswith('_'))
    json.dump({'keys': keys, 'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'count': len(keys)},
              open(os.path.join(args.out_dir, '_manifest.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('清单：%d 篇有表格旁路' % len(keys))
    print('完成：', stats, '| 耗时 %.0fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
