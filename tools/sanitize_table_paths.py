# -*- coding: utf-8 -*-
"""清理表格 sidecar 中的本机绝对路径

规则：
  - 删除字段 `source` / `sourcePath` / `path` 等含本机路径的元数据；
  - 统一写 `sourceFile` = PDF 文件名（不含目录）；
  - 其余正文内容（OCR/表格内容里可能出现的 URL、机构名）不动。
用法：python tools/sanitize_table_paths.py [--dir <index>] [--dry-run]
"""
import argparse
import glob
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
LIVE_INDEX = os.path.join(os.environ['APPDATA'], 'Zotero', 'Zotero', 'Profiles',
                          'default', 'retrieval-index')
PATH_KEYS = ('source', 'sourcePath', 'path', 'file', 'localPath', 'absPath', 'pdfPath')
ABS = re.compile(r'^[A-Za-z]:[\\/]|^/[A-Za-z]/|^\\\\')


def scrub(obj, changed):
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k in PATH_KEYS and isinstance(obj[k], str):
                p = obj.pop(k)
                changed.append(k)
                if 'sourceFile' not in obj and os.path.basename(p.replace('\\', '/')) != p:
                    obj['sourceFile'] = os.path.basename(p.replace('\\', '/'))
            else:
                scrub(obj[k], changed)
    elif isinstance(obj, list):
        for x in obj:
            scrub(x, changed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=LIVE_INDEX)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    tdir = os.path.join(args.dir, 'tables')
    files = [f for f in glob.glob(os.path.join(tdir, '*.json')) if not os.path.basename(f).startswith('_')]
    changed_files = 0
    key_counts = {}
    for f in files:
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        changed = []
        scrub(d, changed)
        if changed:
            changed_files += 1
            for k in changed:
                key_counts[k] = key_counts.get(k, 0) + 1
            if not args.dry_run:
                json.dump(d, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('表格 sidecar: %d 个文件，含本机路径字段并已清理: %d 个' % (len(files), changed_files))
    print('清理字段计数:', key_counts)
    if args.dry_run:
        print('（dry-run，未写入）')


if __name__ == '__main__':
    main()
