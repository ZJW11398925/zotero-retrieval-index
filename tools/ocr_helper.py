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
import glob
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
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

# OCR 质量判据（中文常用字覆盖率 + 中文 bigram 命中率）
# 背景：Windows zh-CN 引擎读韩/日文会输出「像汉字但全是罕见字」的乱码；原语言探针只按汉字占比判断会误判。
# 实测：真中文 bigram≥0.43、常用字≥0.97；韩文 bigram≤0.37、日文≈0.63（日文由跳过清单人工排除）。
COMMON_CHARS = set()
BIGRAMS = set()


def _load_lexicon():
    global COMMON_CHARS, BIGRAMS
    try:
        p = os.path.join(TOOLS_DIR, 'cjk_common_chars.txt')
        if os.path.exists(p):
            COMMON_CHARS = set(open(p, encoding='utf-8').read())
    except Exception:
        pass
    try:
        p = os.path.join(TOOLS_DIR, 'cjk_bigrams.txt')
        if os.path.exists(p):
            BIGRAMS = set(open(p, encoding='utf-8').read().split('\n'))
    except Exception:
        pass


def ocr_quality(text):
    """返回 (bigram 命中率, 常用字覆盖率)；汉字不足 30 时返回 (None, None)。"""
    s = str(text or '')
    chars = [ch for ch in s if 0x4E00 <= ord(ch) <= 0x9FFF]
    if len(chars) < 30:
        return None, None
    hit = tot = 0
    prev = None
    for ch in s:
        if 0x4E00 <= ord(ch) <= 0x9FFF:
            if prev is not None:
                tot += 1
                if (prev + ch) in BIGRAMS:
                    hit += 1
            prev = ch
        else:
            prev = None
    common = sum(1 for ch in chars if ch in COMMON_CHARS) / len(chars)
    return ((hit / tot) if tot else None), common


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


def ocr_image(ps_script, png_path, lang='zh-CN'):
    r = subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps_script,
         '-ImagePath', png_path, '-Lang', lang],
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


def scan_storage(dpi_hint=''):
    """不依赖插件统计，直接扫 storage 找“无文本”PDF（无需先跑一次全量重建）"""
    import pymupdf
    no_text, partial = [], []
    for d in glob.glob(os.path.join(STORAGE, '*')):
        pdfs = glob.glob(os.path.join(d, '*.pdf'))
        if not pdfs:
            continue
        key = os.path.basename(d)
        try:
            doc = pymupdf.open(pdfs[0])
            n = len(doc)
            texts = [doc[i].get_text() for i in range(n)]
            doc.close()
        except Exception:
            continue
        chars = sum(len((t or '').replace(' ', '')) for t in texts)
        blanks = [i + 1 for i, t in enumerate(texts) if len((t or '').replace(' ', '')) < 50]
        if chars < 500:
            no_text.append({'itemKey': key, 'title': os.path.basename(pdfs[0])[:70], 'pages': n})
        elif blanks and len(blanks) < n:
            partial.append({'itemKey': key, 'pages': blanks})
    return no_text, partial


def ocr_pdf(pdf_path, ps_script, only_pages=None, fallback_texts=None, min_chars=50, dpi=200, lang='zh-CN'):
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
                txt = clean(ocr_image(ps_script, png, lang))
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


def script_ratio(text):
    """返回 (中文占比, 韩文占比, 日文假名占比)——按字符数估算"""
    import unicodedata
    if not text:
        return 0.0, 0.0, 0.0
    han = hangul = kana = total = 0
    for ch in text:
        o = ord(ch)
        if ch.isspace() or unicodedata.category(ch).startswith('P'):
            continue
        total += 1
        if 0xAC00 <= o <= 0xD7A3:
            hangul += 1
        elif 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0x4E00 <= o <= 0x9FFF:
            han += 1
    if not total:
        return 0.0, 0.0, 0.0
    return han / total, hangul / total, kana / total


def lang_gate(text, lang):
    """按目标 OCR 语言校验整篇文本；返回 (是否通过, 细节字符串)。

    中文：bigram ≥0.45 且常用字 ≥0.90（阈值由调用方另传，这里只做非中文语言）
    韩语（ko）：Hangul 占比 ≥0.10（韩文论文常混汉字/英文，0.10 是保守下限）
    日语（ja）：假名占比 ≥0.03（日语正文必有假名，可区分「把日文当中文读」的乱码）
    英语（en）：拉丁字母占非空白非标点字符 ≥0.50
    """
    han, hangul, kana = script_ratio(text)
    if lang.startswith('ko'):
        return (hangul >= 0.10), 'hangul=%.3f han=%.3f' % (hangul, han)
    if lang.startswith('ja'):
        return (kana >= 0.03), 'kana=%.3f han=%.3f' % (kana, han)
    if lang.startswith('en'):
        import unicodedata
        letters = tot = 0
        for ch in str(text or ''):
            if ch.isspace() or unicodedata.category(ch).startswith('P'):
                continue
            tot += 1
            if ch.isalpha() and ord(ch) < 0x2E80:
                letters += 1
        ratio = (letters / tot) if tot else 0.0
        return (ratio >= 0.50), 'latin=%.3f' % ratio
    return (None, 'zh 由 bigram/常用字闸负责')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--only-blank', action='store_true', help='只补空白页')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dpi', type=int, default=200)
    ap.add_argument('--scan', action='store_true', help='不依赖插件统计，直接扫 storage 找候选')
    ap.add_argument('--keys', default='', help='逗号分隔的 itemKey，限定只处理这些')
    ap.add_argument('--max-pages', type=int, default=0, help='每篇最多 OCR 前 N 页（0=全部）')
    ap.add_argument('--probe-lang', action='store_true',
                    help='先 OCR 首页探测文字系统，只处理中文为主的文献（跳过韩/日文）')
    ap.add_argument('--force', action='store_true', help='重做已有旁路')
    ap.add_argument('--budget-pages', type=int, default=0, help='累计 OCR 页数上限（0=不限）')
    ap.add_argument('--min-bigram', type=float, default=0.45, help='全文 OCR 的中文 bigram 命中率下限（0=关闭）')
    ap.add_argument('--min-common', type=float, default=0.90, help='全文 OCR 的常用字覆盖率下限（0=关闭）')
    ap.add_argument('--lang', default='zh-CN', help='OCR 语言：zh-CN / ko-KR / ja-JP（需系统已装对应 OCR 能力）')
    ap.add_argument('--skip-file', default='', help='跳过清单 JSON（{itemKey: reason}）；质量不达标的会自动追加')
    args = ap.parse_args()

    _load_lexicon()
    skip = {}
    if args.skip_file and os.path.exists(args.skip_file):
        try:
            d = json.load(open(args.skip_file, encoding='utf-8'))
            skip = d if isinstance(d, dict) else {k: 'skip' for k in d}
        except Exception:
            skip = {}

    def mark_skip(key, reason):
        if not args.skip_file:
            return
        skip[key] = reason
        try:
            with open(args.skip_file, 'w', encoding='utf-8') as f:
                json.dump(skip, f, ensure_ascii=False, indent=1, sort_keys=True)
        except Exception:
            pass

    want = set(k.strip() for k in args.keys.split(',') if k.strip())
    if want and args.scan:
        # 显式指定 keys 时无需扫库，直接按“整篇 OCR”建任务
        failed = [{'itemKey': k, 'title': ''} for k in want]
        blank = {}
        ocr_dir = os.path.join(os.environ['APPDATA'], 'Zotero', 'Zotero', 'Profiles',
                               'xs8xzls2.default', 'retrieval-index', 'ocr')
    elif args.scan:
        print('扫描 storage（约 2 分钟）...')
        failed, blank_list = scan_storage()
        blank = {it['itemKey']: it['pages'] for it in blank_list}
        ocr_dir = os.path.join(os.environ['APPDATA'], 'Zotero', 'Zotero', 'Profiles',
                               'xs8xzls2.default', 'retrieval-index', 'ocr')
    else:
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

    if want:
        jobs = {k: v for k, v in jobs.items() if k in want}

    if skip:
        before = len(jobs)
        jobs = {k: v for k, v in jobs.items() if k not in skip}
        print('按跳过清单排除 %d 篇（清单共 %d 条）' % (before - len(jobs), len(skip)))

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
    pages_done = 0
    lang_skipped = []
    t_all = time.time()
    for idx, (key, job) in enumerate(jobs.items(), 1):
        pdf = find_pdf(key)
        if not pdf:
            print('  [%d/%d] %s 跳过：找不到 PDF' % (idx, len(jobs), key))
            skipped += 1
            continue
        if args.budget_pages and pages_done >= args.budget_pages:
            print('  已达页数预算 %d，停止' % args.budget_pages)
            break
        # 已有旁路文件则跳过（除非 --force）
        _out = os.path.join(ocr_dir, key + '.json')
        if os.path.exists(_out) and not getattr(args, 'force', False):
            print('  [%d/%d] %s 跳过：已有旁路' % (idx, len(jobs), key))
            skipped += 1
            continue
        t0 = time.time()
        try:
            # 语言探测：只处理中文为主的文献（zh-CN 引擎读不了韩/日）
            if args.probe_lang:
                import pymupdf as _pm2
                _dp = _pm2.open(pdf)
                _png = os.path.join(TMP, 'probe_%s.png' % key)
                _dp[0].get_pixmap(dpi=args.dpi).save(_png)
                _dp.close()
                try:
                    _ptxt = clean(ocr_image(ps_script, _png))
                finally:
                    try:
                        os.remove(_png)
                    except OSError:
                        pass
                han, hangul, kana = script_ratio(_ptxt)
                if hangul > 0.15 or kana > 0.10 or han < 0.25:
                    lang_skipped.append({'key': key, 'han': round(han, 2),
                                         'hangul': round(hangul, 2), 'kana': round(kana, 2)})
                    print('  [%d/%d] %s 跳过：非中文文档（汉%.2f 韩%.2f 假名%.2f）' % (
                        idx, len(jobs), key, han, hangul, kana))
                    skipped += 1
                    continue
            if job['mode'] == 'full':
                only = None
                if args.max_pages > 0:
                    import pymupdf as _pm
                    _d = _pm.open(pdf)
                    _n = min(args.max_pages, len(_d))
                    _d.close()
                    only = set(range(1, _n + 1))
                pages, ocr_pages = ocr_pdf(pdf, ps_script, only_pages=only, dpi=args.dpi, lang=args.lang)
                # 质量闸：中文用 bigram/常用字；韩/日按目标语言的字形占比校验，拒绝乱码入库
                if args.lang.startswith('zh'):
                    if args.min_bigram or args.min_common:
                        _bg, _cm = ocr_quality('\n'.join(pages))
                        if _bg is not None and ((args.min_bigram and _bg < args.min_bigram) or
                                                (args.min_common and _cm < args.min_common)):
                            mark_skip(key, 'OCR 质量不达标 bigram=%.3f common=%.3f（疑似韩/日文）' % (_bg, _cm))
                            print('  [%d/%d] %s 跳过：OCR 质量不达标（bigram=%.3f common=%.3f，已记入跳过清单）' % (
                                idx, len(jobs), key, _bg, _cm))
                            skipped += 1
                            continue
                else:
                    _ok, _detail = lang_gate('\n'.join(pages), args.lang)
                    if _ok is False:
                        mark_skip(key, 'OCR(%s) 语言校验不达标 %s' % (args.lang, _detail))
                        print('  [%d/%d] %s 跳过：OCR(%s) 语言校验不达标（%s，已记入跳过清单）' % (
                            idx, len(jobs), key, args.lang, _detail))
                        skipped += 1
                        continue
            else:
                pages, ocr_pages = ocr_pdf(pdf, ps_script, only_pages=set(job['pages']), dpi=args.dpi, lang=args.lang)
            out = os.path.join(ocr_dir, key + '.json')
            with open(out, 'w', encoding='utf-8') as f:
                json.dump({'pages': pages, 'ocrPages': ocr_pages}, f, ensure_ascii=False)
            if key not in keys:
                keys.append(key)
            # 每篇写完立刻更新 manifest：进程/会话中断也不会出现「有旁路但没登记」的孤儿文件
            try:
                with open(manifest_path, 'w', encoding='utf-8') as _mf:
                    json.dump({'keys': keys, 'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S')},
                              _mf, ensure_ascii=False)
            except Exception:
                pass
            pages_done += len(pages)
            ok += 1
            print('  [%d/%d] %s %s：%d 页，OCR %d 页，%ds → %s' % (
                idx, len(jobs), key, job['mode'], len(pages), len(ocr_pages),
                int(time.time() - t0), os.path.basename(out)))
        except Exception as e:
            failed_n += 1
            print('  [%d/%d] %s 失败：%s' % (idx, len(jobs), key, e))

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump({'keys': keys, 'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S')}, f, ensure_ascii=False)

    print('\n完成：成功 %d / 跳过 %d（其中非中文 %d）/ 失败 %d，累计 OCR %d 页，总耗时 %ds' % (
        ok, skipped, len(lang_skipped), failed_n, pages_done, int(time.time() - t_all)))
    if lang_skipped:
        print('非中文跳过清单（zh-CN 引擎读不了韩/日文）：%s' % (
            ', '.join(x['key'] for x in lang_skipped[:20]) + (' …' if len(lang_skipped) > 20 else '')))
    print('旁路目录：%s' % ocr_dir)
    print('下一步：Zotero → 工具 → 增量更新索引（或 GET /retrieval/build?incremental=1）')


if __name__ == '__main__':
    main()
