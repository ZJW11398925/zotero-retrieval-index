# -*- coding: utf-8 -*-
"""表格跨页合并：对已有 sidecar（tables/<key>.json）做保守合并，写回并统计

保守合并规则（同列数、页差 1–2，且满足任一）：
  a. 下一表题注含“续 / continued”；
  b. 下一表题注为空且上一表题注非空（典型：长表跨页，第二页不重复表题/表头）；
  c. 两表题注含相同表号（如 “表 4-1” / “Table 2”）；
  d. 两表表头完全相同（重复表头，去掉下一表的表头行）。
合并后：page 取起始页，pages=[start,end]，nrows 累加；markdown 重拼。

用法：python tools/merge_tables.py [--index-dir ...] [--dry-run]
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_paths import find_index_dir  # noqa: E402

LIVE_INDEX = find_index_dir()
CAPNUM = re.compile(r'(表|Table)\s*([0-9]+(?:[-–.][0-9]+)?)', re.I)
CONT = re.compile(r'续|continued', re.I)


TOTAL_RE = re.compile(r'^(总计|合计|小计|total|sum)\b|总计|合计|小计', re.I)


def rows_of(md):
    out = []
    for ln in str(md or '').split('\n'):
        s = ln.strip()
        if not s.startswith('|'):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if cells and all(re.fullmatch(r'-{3,}', c or '') for c in cells):
            continue
        out.append(cells)
    return out


def cap_num(cap):
    m = CAPNUM.search(cap or '')
    return (m.group(1).lower(), m.group(2)) if m else None


def can_merge(a, b):
    pa, pb = int(a.get('page') or 0), int(b.get('page') or 0)
    if pa <= 0 or pb <= 0 or not (1 <= pb - pa <= 2):
        return None
    if int(a.get('ncols') or 0) != int(b.get('ncols') or 0) or int(a.get('ncols') or 0) < 2:
        return None
    ca, cb = (a.get('caption') or '').strip(), (b.get('caption') or '').strip()
    ra, rb = rows_of(a.get('markdown')), rows_of(b.get('markdown'))
    if not ra or not rb:
        return None
    if CONT.search(cb):
        return 'continued'
    na, nb = cap_num(ca), cap_num(cb)
    if na and nb and na == nb:
        return 'same-number'
    # b 无题注：典型跨页续表；但若 a 以“总计/合计”收尾，说明 a 是完整表，不合并
    if not cb and ca and not TOTAL_RE.search(' '.join(ra[-1])):
        if int(b.get('nrows') or 0) >= 2:
            return 'empty-caption'
    return None


def merge_two(a, b, reason):
    ra, rb = rows_of(a.get('markdown')), rows_of(b.get('markdown'))
    if not ra or not rb:
        return None
    header = ra[0]
    body_a = ra[1:]
    # 若下一表重复了表头，去掉重复表头行
    body_b = rb[1:] if rb[0] == header else rb
    ncols = max(len(header), *(len(r) for r in body_a + body_b))
    header = header + [''] * (ncols - len(header))
    body = [r + [''] * (ncols - len(r)) for r in body_a + body_b]
    md = '| ' + ' | '.join(header) + ' |\n| ' + ' | '.join(['---'] * ncols) + ' |\n'
    md += '\n'.join('| ' + ' | '.join(r) + ' |' for r in body)
    p0, p1 = int(a.get('page') or 0), int(b.get('page') or 0)
    return {
        'page': p0, 'pages': [p0, p1],
        'caption': a.get('caption') or b.get('caption') or '',
        'markdown': md, 'nrows': len(body), 'ncols': ncols,
        'mergedFrom': [p0, p1], 'mergeReason': reason,
    }


def merge_tables(tabs):
    out = []
    merges = []
    for t in tabs:
        if out:
            reason = can_merge(out[-1], t)
            if reason:
                m = merge_two(out[-1], t, reason)
                if m:
                    out[-1] = m
                    merges.append(reason)
                    continue
        out.append(t)
    return out, merges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index-dir', default=LIVE_INDEX)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--backup-dir', default=os.path.join(os.environ.get('USERPROFILE', '.'), 'zotero', 'dist', 'backup', 'tables_before_merge'))
    args = ap.parse_args()
    tdir = os.path.join(args.index_dir, 'tables')
    files = [f for f in glob.glob(os.path.join(tdir, '*.json')) if not os.path.basename(f).startswith('_')]
    if not args.dry_run:
        os.makedirs(args.backup_dir, exist_ok=True)
    stats = {'files': 0, 'changed': 0, 'merges': 0, 'tables_before': 0, 'tables_after': 0}
    examples = []
    for f in files:
        stats['files'] += 1
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        tabs = d.get('tables') or []
        stats['tables_before'] += len(tabs)
        merged, merges = merge_tables(tabs)
        stats['tables_after'] += len(merged)
        if merges:
            stats['changed'] += 1
            stats['merges'] += len(merges)
            if len(examples) < 5 and merged:
                for m in merged:
                    if m.get('mergedFrom'):
                        examples.append((os.path.basename(f)[:-5], m))
                        break
            if not args.dry_run:
                shutil.copy2(f, os.path.join(args.backup_dir, os.path.basename(f)))
                d['tables'] = merged
                d['mergedAt'] = __import__('time').strftime('%Y-%m-%dT%H:%M:%S')
                json.dump(d, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('文件：%d | 变更：%d | 合并次数：%d | 表数：%d → %d' % (
        stats['files'], stats['changed'], stats['merges'], stats['tables_before'], stats['tables_after']))
    for key, m in examples:
        print('\n样例 %s: %s 页 %s 行数 %s 列数 %s' % (
            key, m.get('caption', '')[:40], m.get('pages'), m.get('nrows'), m.get('ncols')))
        print(m.get('markdown', '')[:420])


if __name__ == '__main__':
    main()
