# -*- coding: utf-8 -*-
"""打包脚本：读取 manifest.json 的版本号，生成 retrieval-index-<version>.xpi

用法： python build.py
"""
import json
import os
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
INCLUDE = [
    'manifest.json',
    'bootstrap.js',
    'prefs.js',
    'README.md',
    'LICENSE',
    'content/core.js',
    'content/retrieval.js',
    'chrome/content/icons/favicon.png',
    'chrome/content/icons/favicon@0.5x.png',
]


def main():
    manifest = json.load(open(os.path.join(ROOT, 'manifest.json'), encoding='utf-8'))
    version = manifest['version']
    plugin_id = manifest['applications']['zotero']['id']

    # 校验必填项（Zotero 10 的清单校验会拒绝缺失 update_url 的包）
    for key in ('id', 'update_url', 'strict_min_version', 'strict_max_version'):
        if key not in manifest['applications']['zotero']:
            raise SystemExit('manifest.json 缺少 applications.zotero.%s' % key)

    out = os.path.join(ROOT, 'retrieval-index-%s.xpi' % version)
    if os.path.exists(out):
        os.remove(out)

    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in INCLUDE:
            path = os.path.join(ROOT, rel.replace('/', os.sep))
            if not os.path.exists(path):
                raise SystemExit('缺少文件：' + rel)
            z.write(path, rel)

    size = os.path.getsize(out) / 1024
    print('已生成 %s (%.1f KB)' % (out, size))
    print('插件 ID : %s' % plugin_id)
    print('版本    : %s' % version)
    print('更新地址: %s' % manifest['applications']['zotero']['update_url'])


if __name__ == '__main__':
    main()
