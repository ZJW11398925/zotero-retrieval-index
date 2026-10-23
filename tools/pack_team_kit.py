# -*- coding: utf-8 -*-
"""打包「团队分发包」：插件 xpi + 索引快照 + OCR 旁路 + 部署/校验脚本 + 使用说明

用法（在仓库根目录或任意位置）：
    python tools/pack_team_kit.py                     # 用当前 live 索引 + 仓库里的最新 xpi
    python tools/pack_team_kit.py --index-dir D:\\zotero\\idx_sit_only
    python tools/pack_team_kit.py --out-dir D:\\zotero\\dist --date 20261101

产物：<out-dir>/retrieval-index-team-kit-v<version>-<date>.zip
      <out-dir>/retrieval-index-team-kit-v<version>/        （未压缩的分发目录）

约定（2026-10-23 与用户确认）：
  * 渠道：所内私有网盘/群文件（索引含文献正文，禁止公开上传）
  * 更新：每月重新打一次全量快照，队友覆盖 index 目录即可
"""
import argparse
import datetime
import hashlib
import json
import os
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index-dir', default=LIVE_INDEX)
    ap.add_argument('--out-dir', default=os.path.join(os.path.dirname(ROOT), 'dist'))
    ap.add_argument('--xpi', default='')
    ap.add_argument('--date', default=datetime.date.today().strftime('%Y%m%d'))
    args = ap.parse_args()

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

    # 1) 插件 + 索引 + OCR 旁路
    shutil.copy2(xpi, os.path.join(staging, os.path.basename(xpi)))
    shutil.copytree(idx, os.path.join(staging, 'index'),
                    ignore=shutil.ignore_patterns('*.bak-v*', 'chunks.json.bak*'))
    print('已复制索引（%d 文件）' % sum(len(fs) for _, _, fs in os.walk(os.path.join(staging, 'index'))))

    # 2) 部署/校验脚本（ps1 加 BOM 适配 Windows PowerShell 5.1）+ 说明
    copy_ps1_bom(os.path.join(KIT_SRC, 'deploy_index.ps1'), os.path.join(staging, 'deploy_index.ps1'))
    copy_ps1_bom(os.path.join(KIT_SRC, 'verify_install.ps1'), os.path.join(staging, 'verify_install.ps1'))
    readme = open(os.path.join(KIT_SRC, '使用说明-团队分发.md'), encoding='utf-8').read()
    nice_date = '%s-%s-%s' % (args.date[:4], args.date[4:6], args.date[6:8]) if len(args.date) == 8 else args.date
    readme = readme.replace('<快照日期>', nice_date)
    open(os.path.join(staging, '使用说明-团队分发.md'), 'w', encoding='utf-8', newline='\n').write(readme)
    print('说明已生成（快照日期 %s）' % nice_date)

    # 3) manifest（含 OCR 树哈希）
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
    meta = json.load(open(os.path.join(idx, 'meta.json'), encoding='utf-8'))
    chunk_count = len(json.load(open(os.path.join(idx, 'chunks.json'), encoding='utf-8')))
    man = {
        'schema': 1,
        'generatedAt': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'pluginVersion': version,
        'snapshotLabel': args.date,
        'files': files,
        'ocr': {'count': len(entries), 'treeSha256': tree,
                'files': [{'name': n, 'sha256': h, 'bytes': b} for n, h, b in entries]},
        'index': {
            'builtAt': meta.get('builtAt'), 'chunks': chunk_count,
            'parents': meta.get('parents'), 'dim': meta.get('dim'), 'model': meta.get('model'),
            'orphanChunks': meta.get('orphanChunks'), 'ocrChunks': meta.get('ocrChunks'),
            'crossLibDropped': meta.get('crossLibDropped'), 'retractedPapers': meta.get('retractedPapers'),
        },
        'note': '索引快照 %s；内部流转，勿公开上传。每月重打一次全量快照，队友覆盖 index 目录即可。' % args.date,
    }
    json.dump(man, open(os.path.join(staging, 'index-manifest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    # 4) 压缩
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
    print('索引        : %s 块 / %s 父条目（builtAt %s）' % (man['index']['chunks'], man['index']['parents'], man['index']['builtAt']))
    print('OCR 旁路    : %d 个文件' % len(entries))
    print('原始大小    : %.1f MB' % (raw_total / 1048576))
    print('压缩包      : %s' % out_zip)
    print('压缩后      : %.1f MB' % (os.path.getsize(out_zip) / 1048576))
    print('zip sha256  : %s' % sha256(out_zip))
    print('')
    print('下一步：把 zip 传到所内私有网盘/群文件；队友按《使用说明-团队分发.md》三步安装。')


if __name__ == '__main__':
    main()
