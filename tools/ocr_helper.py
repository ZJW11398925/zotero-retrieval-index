#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OCR 辅助脚本（Windows 内置 OCR，零外部依赖）

用途：把「没有文本层的 PDF」或「PDF 中的空白页」用 Windows 内置 OCR 识别出来，
      写成插件可读取的旁路文件，再由插件「增量更新索引」并入检索。

前置：
  - Zotero 正在运行，且已装检索索引插件（v0.1.13+）
  - Windows 已安装对应语言的 OCR 语言包（本机 zh-CN 已装）
  - Python 依赖：pymupdf（已随环境提供）

用法：
  python tools/ocr_helper.py                 # 处理全部候选
  python tools/ocr_helper.py --limit 3       # 只处理前 3 篇（试跑）
  python tools/ocr_helper.py --only-blank    # 只补空白页，不处理整篇无文本的
  python tools/ocr_helper.py --dry-run       # 只列候选，不实际 OCR

产物（写入插件数据目录）：
  ocr/_manifest.json      {"keys": [...]}
  ocr/<itemKey>.json      {"pages": ["第1页文本", ...], "ocrPages": [1,3,...]}
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ENDPOINT = 'http://127.0.0.1:23119/retrieval/ocr-candidates'
STORAGE = os.path.join(os.environ['USERPROFILE'], 'Zotero', 'storage')
TMP = os.path.join(tempfile.gettempdir(), 'zotero_ocr')
CJK = r'\u4e00-\u9fff'


# ---------- Windows OCR 封装 ----------
PS_CODE = r'''
param([string]$ImagePath, [string]$Lang = 'zh-CN')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
    $asTask = $asTaskGeneric.MakeGenericMethod($type)
    $t = $asTask.Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$language = New-Object Windows.Globalization.Language($Lang)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { Write-Error "OCR engine unavailable for $Lang"; exit 2 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$result.Text
'''


def make_ps_script():
    os.makedirs(TMP, exist_ok=True)
    path = os.path.join(TMP, 'win_ocr.ps1')
    with open(path, 'w', encoding='utf-8-sig') as f:
        f.write(PS_CODE)
    return path


def ocr_image(ps_script, png_path):
    r = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps_script,
         '-ImagePath', png_path],
        capture_output=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError('OCR failed: ' + r.stderr.decode('utf-8', 'replace')[:200])
    return r.stdout.decode('utf-8', 'replace')


def clean(text):
    """合并汉字之间的空格（Windows OCR 会在每个汉字间插空格，直接分词会碎掉）"""
    if not text:
        return ''
    text = text.replace('\r\n', '\n')
    text = re.sub(r'(?<=[' + CJK + r'])[ \t]+(?=[' + CJK + r'])', '', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# ---------- 主流程 ----------
def fetch_candidates():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(ENDPOINT, timeout=60) as r:
        return json.loads(r.read().decode('utf-8'))


def find_pdf(item_key):
    d = os.path.join(STORAGE, item_key)
    if not os.path.isdir(d):
        return None
    for f in os.listdir(d):
        if f.lower().endswith('.pdf'):
            return os.path.join(d, f)
    return None


def ocr_pdf(pdf_path, ps_script, only_pages=None, fallback_texts=None, min_chars=50, dpi=200):
    """返回 (pages, ocr_pages)。only_pages=None 表示整篇 OCR。"""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    n = len(doc)
    pages, ocr_pages = [], []
    for i in range(n):
        page_no = i + 1
        need = (only_pages is None) or (page_no in only_pages)
        if need:
            pix = doc[i].get_pixmap(dpi=dpi)
            png = os.path.join(TMP, 'p%05d.png' % page_no)
            pix.save(png)
            try:
                txt = clean(ocr_image(ps_script, png))
            finally:
                try:
                    os.remove(png)
                except OSError:
                    pass
            if not txt and fallback_texts:
                txt = fallback_texts[i] if i < len(fallback_texts) else ''
            pages.append(txt)
            ocr_pages.append(page_no)
        else:
            txt = doc[i].get_text()
            if (fallback_texts and i < len(fallback_texts)
                    and len((txt or '').replace(' ', '')) < min_chars):
                txt = fallback_texts[i]
            pages.append(txt)
    doc.close()
    return pages, ocr_pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only-blank', action='store_true', help='只补空白页')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dpi', type=int, default=200)
    args = ap.parse_args()

    data = fetch_candidates()
    ocr_dir = data['ocrDir']
    failed = data.get('failed') or []
    blank = data.get('blankPages') or {}

    jobs = {}
    if not args.only_blank:
        for it in failed:
            jobs[it['itemKey']] = {'mode': 'full', 'title': it.get('title', '')}
    for k, pages in blank.items():
        if k in jobs:
            continue
        jobs[k] = {'mode': 'blank', 'pages': pages, 'title': ''}

    print('候选：整篇无文本 %d 篇 / 含空白页 %d 篇 → 合计 %d 篇' % (len(failed), len(blank), len(jobs)))
    if args.limit:
        jobs = dict(list(jobs.items())[:args.limit])

    os.makedirs(ocr_dir, exist_ok=True)
    manifest_path = os.path.join(ocr_dir, '_manifest.json')
    keys = []
    if os.path.exists(manifest_path):
        try:
            keys = json.load(open(manifest_path, encoding='utf-8')).get('keys', [])
        except Exception:
            keys = []

    if args.dry_run:
        for k, j in jobs.items():
            print('  %s [%s] %s' % (k, j['mode'], j['title'][:40]))
        return

    ps_script = make_ps_script()
    ok = skipped = failed_n = 0
    t_all = time.time()
    for idx, (key, job) in enumerate(jobs.items(), 1):
        pdf = find_pdf(key)
        if not pdf:
            print('  [%d/%d] %s 跳过：找不到 PDF' % (idx, len(jobs), key))
            skipped += 1
            continue
        t0 = time.time()
        try:
            if job['mode'] == 'full':
                pages, ocr_pages = ocr_pdf(pdf, ps_script, only_pages=None, dpi=args.dpi)
            else:
                pages, ocr_pages = ocr_pdf(pdf, ps_script, only_pages=set(job['pages']), dpi=args.dpi)
            out = os.path.join(ocr_dir, key + '.json')
            with open(out, 'w', encoding='utf-8') as f:
                json.dump({'pages': pages, 'ocrPages': ocr_pages}, f, ensure_ascii=False)
            if key not in keys:
                keys.append(key)
            ok += 1
            print('  [%d/%d] %s %s：%d 页，OCR %d 页，%ds → %s' % (
                idx, len(jobs), key, job['mode'], len(pages), len(ocr_pages),
                int(time.time() - t0), os.path.basename(out)))
        except Exception as e:
            failed_n += 1
            print('  [%d/%d] %s 失败：%s' % (idx, len(jobs), key, e))

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({'keys': keys, 'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S')}, f, ensure_ascii=False)

    print('\n完成：成功 %d / 跳过 %d / 失败 %d，总耗时 %ds' % (
        ok, skipped, failed_n, int(time.time() - t_all)))
    print('旁路目录：%s' % ocr_dir)
    print('下一步：Zotero → 工具 → 增量更新索引（或 GET /retrieval/build?incremental=1）')


if __name__ == '__main__':
    main()
