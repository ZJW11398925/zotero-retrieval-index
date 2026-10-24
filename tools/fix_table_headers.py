# -*- coding: utf-8 -*-
"""表头多行合并：把被压成数据行的多行表头并回表头（对已有 sidecar 后处理）

规则（保守）：
  - 取前 2 行；若两行都"像表头"（无数字、单元格短、含空单元格）且第 3 行有数字，
    则把前两行合并为一行表头（同列非空片段按上→下拼接、去重）；
  - 单行表头不动。
输出：原地更新 tables/*.json（先备份），打印统计与样例。
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
NUM = re.compile(r'^\s*[-+]?\d+(?:[.,]\d+)?\s*%?\s*$')


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


def pad(row, n):
    return list(row) + [''] * (n - len(row))


def header_like(row):
    if not row:
        return False
    nonempty = [c for c in row if c]
    if not nonempty:
        return False
    if any(NUM.match(c) for c in nonempty):
        return False
    if any(len(c) > 12 for c in nonempty):
        return False
    return True


def has_digits(row):
    return any(re.search(r'\d', c) for c in (row or []))


def try_merge_header(rows):
    if len(rows) < 3:
        return None
    ncols = max(len(r) for r in rows)
    # 列数过多通常是解析乱序（散词被当成列），不合并
    if ncols > 8:
        return None
    a = pad(rows[0], ncols)
    b = pad(rows[1], ncols)
    if not (header_like(a) and header_like(b)):
        return None
    # 子表头（第二行非空单元格）必须是短标签
    if any(len(c) > 8 for c in b if c):
        return None
    # 首行非空单元格也不能过长（分组名一般很短）
    if any(len(c) > 10 for c in a if c):
        return None
    # 至少一行含空单元格（合并单元格展开的典型特征）
    if sum(1 for c in a if not c) == 0 and sum(1 for c in b if not c) == 0:
        return None
    # 第三行起至少要有一行像数据（含数字或长文本），否则可能是三行表头，本脚本不处理
    rest = rows[2:]
    if not rest:
        return None
    if not any(has_digits(r) or any(len(c) > 12 for c in r) for r in rest):
        return None
    header = []
    for j in range(ncols):
        parts = []
        for r in (a, b):
            c = r[j]
            if c and (not parts or parts[-1] != c):
                parts.append(c)
        header.append(' '.join(parts))
    body = [pad(r, ncols) for r in rest]
    return header, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index-dir', default=LIVE_INDEX)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--backup-dir', default=os.path.join(os.environ.get('USERPROFILE', '.'), 'zotero', 'dist', 'backup', 'tables_before_headerfix'))
    args = ap.parse_args()
    tdir = os.path.join(args.index_dir, 'tables')
    files = [f for f in glob.glob(os.path.join(tdir, '*.json')) if not os.path.basename(f).startswith('_')]
    if not args.dry_run:
        os.makedirs(args.backup_dir, exist_ok=True)
    stats = {'files': 0, 'tables': 0, 'fixed': 0, 'docs_changed': 0}
    examples = []
    for f in files:
        stats['files'] += 1
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        changed = False
        tabs = d.get('tables') or []
        for t in tabs:
            stats['tables'] += 1
            rows = rows_of(t.get('markdown'))
            if len(rows) < 3:
                continue
            m = try_merge_header(rows)
            if not m:
                continue
            header, body = m
            ncols = len(header)
            md = '| ' + ' | '.join(header) + ' |\n| ' + ' | '.join(['---'] * ncols) + ' |\n'
            md += '\n'.join('| ' + ' | '.join(pad(r, ncols)) + ' |' for r in body)
            if len(examples) < 8:
                examples.append((os.path.basename(f)[:-5], t.get('page'), header[:6]))
            t['markdown'] = md
            t['nrows'] = len(body)
            t['ncols'] = ncols
            t['headerFixed'] = True
            stats['fixed'] += 1
            changed = True
        if changed:
            stats['docs_changed'] += 1
            if not args.dry_run:
                shutil.copy2(f, os.path.join(args.backup_dir, os.path.basename(f)))
                json.dump(d, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('文件 %d | 表格 %d | 修复表头 %d | 变更文档 %d' % (
        stats['files'], stats['tables'], stats['fixed'], stats['docs_changed']))
    for key, page, head in examples:
        print('  例: %s p%s 表头 → %s' % (key, page, ' | '.join(head)))


if __name__ == '__main__':
    main()
