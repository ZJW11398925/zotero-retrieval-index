# -*- coding: utf-8 -*-
"""打包「团队分发包」：插件 xpi + 索引快照 + OCR 旁路 + 部署/校验脚本 + 使用说明

用法（在仓库根目录或任意位置）：
    python tools/pack_team_kit.py                                   # live 索引 + 仓库最新 xpi
    python tools/pack_team_kit.py --exclude-libraries 1             # 纯净版：剔除个人库（lib1）
    python tools/pack_team_kit.py --index-dir D:\\zotero\\idx_sit_only
    python tools/pack_team_kit.py --out-dir D:\\zotero\\dist --date 20261101

产物：<out-dir>/retrieval-index-team-kit-v<version>-<date>.zip
      <out-dir>/retrieval-index-team-kit-v<version>/        （未压缩的分发目录）

约定（2026-10-23 与用户确认）：
  * 渠道：所内私有网盘/群文件（索引含文献正文，禁止公开上传）
  * 更新：每月重新打一次全量快照，队友覆盖 index 目录即可
  * 纯净版：--exclude-libraries 1 剔除非共享库（个人库 itemKey 在他人机器上不存在）
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_INDEX = os.path.join(os.environ.get('APPDATA', ''), 'Zotero', 'Zotero', 'Profiles',
                          'xs8xzls2.default', 'retrieval-index')
KIT_SRC = os.path.join(ROOT, 'tools', 'team-kit')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def copy_ps1_bom(src, dst):
    raw = open(src, 'rb').read()
    if not raw.startswith(b'\xef\xbb\xbf'):
        raw = b'\xef\xbb\xbf' + raw
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, 'wb').write(raw)


def build_filtered_index(src, dst, exclude_libs):
    """按库过滤索引（chunks + vectors 同步裁剪，meta 重算计数）。"""
    chunks = json.load(open(os.path.join(src, 'chunks.json'), encoding='utf-8'))
    meta = json.load(open(os.path.join(src, 'meta.json'), encoding='utf-8'))
    dim = int(meta.get('dim') or 1024)
    raw = base64.b64decode(open(os.path.join(src, 'vectors.b64'), 'rb').read())
    if len(raw) != len(chunks) * dim:
        raise SystemExit('向量与块数不齐：%d != %d×%d' % (len(raw), len(chunks), dim))

    keep = [i for i, c in enumerate(chunks) if c.get('libraryID') not in exclude_libs]
    kept = [chunks[i] for i in keep]
    kept_vec = b''.join(raw[i * dim:(i + 1) * dim] for i in keep)

    os.makedirs(dst, exist_ok=True)
    json.dump(kept, open(os.path.join(dst, 'chunks.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, separators=(',', ':'))
    open(os.path.join(dst, 'vectors.b64'), 'w').write(base64.b64encode(kept_vec).decode())

    kept_keys = {c['itemKey'] for c in kept}
    parents = {(c.get('parentKey') or c['itemKey']) for c in kept}
    retr = {(c.get('parentKey') or c['itemKey']) for c in kept if c.get('retracted')}
    m = dict(meta)
    m['parents'] = len(parents)
    m['papersById'] = len(kept_keys)
    m['orphanChunks'] = sum(1 for c in kept if c.get('orphan'))
    m['ocrChunks'] = sum(1 for c in kept if c.get('ocr') or c.get('pageSource') == 'ocr')
    m['cacheChunks'] = sum(1 for c in kept if c.get('pageSource') == 'cache')
    m['retractedPapers'] = len(retr)
    m['libraries'] = [l for l in (meta.get('libraries') or []) if l.get('id') not in exclude_libs]
    m['itemVersions'] = {k: v for k, v in (meta.get('itemVersions') or {}).items() if k in kept_keys}
    m['ocrUsedKeys'] = [k for k in (meta.get('ocrUsedKeys') or []) if k in kept_keys]
    m['excludedLibraries'] = sorted(exclude_libs)
    json.dump(m, open(os.path.join(dst, 'meta.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, separators=(',', ':'))

    src_ocr = os.path.join(src, 'ocr')
    if os.path.isdir(src_ocr):
        shutil.copytree(src_ocr, os.path.join(dst, 'ocr'), dirs_exist_ok=True)
    print('已过滤索引：%d → %d 块（剔除库 %s），父条目 %d' % (
        len(chunks), len(kept), sorted(exclude_libs), len(parents)))
    return len(kept), len(parents)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index-dir', default=LIVE_INDEX)
    ap.add_argument('--out-dir', default=os.path.join(os.path.dirname(ROOT), 'dist'))
    ap.add_argument('--xpi', default='')
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y%m%d'))
    ap.add_argument('--exclude-libraries', default='', help='逗号分隔的 libraryID，如 "1"（纯净版剔除个人库）')
    args = ap.parse_args()

    exclude = {int(x) for x in re.split(r'[,\s]+', (args.exclude_libraries or '').strip()) if x}

    manifest = json.load(open(os.path.join(ROOT, 'manifest.json'), encoding='utf-8'))
    version = manifest['version']
    xpi = args.xpi or os.path.join(ROOT, 'retrieval-index-%s.xpi' % version)
    if not os.path.exists(xpi):
        raise SystemExit('缺少 xpi：%s（先运行 python build.py）' % xpi)
    idx = args.index_dir
    for f in ('chunks.json', 'vectors.b64', 'meta.json'):
        if not os.path.exists(os.path.join(idx, f)):
            raise SystemExit('索引目录缺少 %s：%s' % (f, idx))

    staging = os.path.join(args.out_dir, 'retrieval-index-team-kit-v%s' % version)
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)

    # 1) 插件 + 索引（可过滤库）+ OCR 旁路
    shutil.copy2(xpi, os.path.join(staging, os.path.basename(xpi)))
    if exclude:
        build_filtered_index(idx, os.path.join(staging, 'index'), exclude)
    else:
        shutil.copytree(idx, os.path.join(staging, 'index'),
                        ignore=shutil.ignore_patterns('*.bak-v*', 'chunks.json.bak*'))
        print('已复制索引（%d 文件）' % sum(len(fs) for _, _, fs in os.walk(os.path.join(staging, 'index'))))

    # 2) 从分发包内索引读取最终计数（过滤后以打包结果为准）
    meta = json.load(open(os.path.join(staging, 'index', 'meta.json'), encoding='utf-8'))
    chunk_count = len(json.load(open(os.path.join(staging, 'index', 'chunks.json'), encoding='utf-8')))
    parent_count = meta.get('parents')

    # 3) 部署/校验脚本（ps1 加 BOM 适配 Windows PowerShell 5.1）+ 说明
    copy_ps1_bom(os.path.join(KIT_SRC, 'deploy_index.ps1'), os.path.join(staging, 'deploy_index.ps1'))
    copy_ps1_bom(os.path.join(KIT_SRC, 'verify_install.ps1'), os.path.join(staging, 'verify_install.ps1'))
    readme = open(os.path.join(KIT_SRC, '使用说明-团队分发.md'), encoding='utf-8').read()
    nice_date = '%s-%s-%s' % (args.date[:4], args.date[4:6], args.date[6:8]) if len(args.date) == 8 else args.date
    readme = (readme.replace('<快照日期>', nice_date)
                    .replace('<块数>', '{:,}'.format(chunk_count))
                    .replace('<篇数>', str(parent_count)))
    open(os.path.join(staging, '使用说明-团队分发.md'), 'w', encoding='utf-8', newline='\n').write(readme)
    print('说明已生成（快照日期 %s；%s 块 / %s 篇）' % (nice_date, chunk_count, parent_count))

    # 4) manifest（含 OCR 树哈希）
    files = {}
    for rel in ('retrieval-index-%s.xpi' % version, 'index/chunks.json', 'index/vectors.b64', 'index/meta.json',
                '使用说明-团队分发.md', 'verify_install.ps1', 'deploy_index.ps1'):
        p = os.path.join(staging, rel.replace('/', os.sep))
        files[rel] = {'sha256': sha256(p), 'bytes': os.path.getsize(p)}
    ocr_dir = os.path.join(staging, 'index', 'ocr')
    entries = []
    if os.path.isdir(ocr_dir):
        for name in sorted(os.listdir(ocr_dir)):
            p = os.path.join(ocr_dir, name)
            if os.path.isfile(p):
                entries.append((name, sha256(p), os.path.getsize(p)))
    tree = hashlib.sha256('\n'.join('%s|%s' % (n, h) for n, h, _ in entries).encode()).hexdigest()
    man = {
        'schema': 1,
        'generatedAt': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'pluginVersion': version,
        'snapshotLabel': args.date,
        'excludedLibraries': sorted(exclude),
        'files': files,
        'ocr': {'count': len(entries), 'treeSha256': tree,
                'files': [{'name': n, 'sha256': h, 'bytes': b} for n, h, b in entries]},
        'index': {
            'builtAt': meta.get('builtAt'), 'chunks': chunk_count,
            'parents': parent_count, 'dim': meta.get('dim'), 'model': meta.get('model'),
            'orphanChunks': meta.get('orphanChunks'), 'ocrChunks': meta.get('ocrChunks'),
            'crossLibDropped': meta.get('crossLibDropped'), 'retractedPapers': meta.get('retractedPapers'),
        },
        'note': '索引快照 %s；内部流转，勿公开上传。每月重打一次全量快照，队友覆盖 index 目录即可。' % args.date,
    }
    if exclude:
        man['note'] += ' 已剔除库 %s（个人库 itemKey 在他人机器上不存在）。' % sorted(exclude)
    json.dump(man, open(os.path.join(staging, 'index-manifest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    # 5) 压缩
    out_zip = os.path.join(args.out_dir, 'retrieval-index-team-kit-v%s-%s.zip' % (version, args.date))
    if os.path.exists(out_zip):
        os.remove(out_zip)
    raw_total = 0
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _, fs in os.walk(staging):
            for fn in fs:
                p = os.path.join(base, fn)
                z.write(p, os.path.join(os.path.basename(staging), os.path.relpath(p, staging)))
                raw_total += os.path.getsize(p)
    print('')
    print('版本        : v%s' % version)
    print('快照日期    : %s' % args.date)
    print('剔除库      : %s' % (sorted(exclude) if exclude else '无（全量）'))
    print('索引        : %s 块 / %s 父条目（builtAt %s）' % (chunk_count, parent_count, meta.get('builtAt')))
    print('OCR 旁路    : %d 个文件' % len(entries))
    print('原始大小    : %.1f MB' % (raw_total / 1048576))
    print('压缩包      : %s' % out_zip)
    print('压缩后      : %.1f MB' % (os.path.getsize(out_zip) / 1048576))
    print('zip sha256  : %s' % sha256(out_zip))
    print('')
    print('下一步：把 zip 传到所内私有网盘/群文件；队友按《使用说明-团队分发.md》三步安装。')


if __name__ == '__main__':
    main()
