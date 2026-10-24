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
    'LICENSE',
    'content/core.js',
    'content/protocol-text.js',
    'content/retrieval.js',
    'chrome/content/icons/favicon.png',
    'chrome/content/icons/favicon@0.5x.png',
]
# 注意：README.md 是开发文档（含本机路径/评测细节），不打进 xpi（分发包隐私要求）。

PROTOCOL_SRC = 'docs/agent-usage-protocol.md'
PROTOCOL_OUT = 'content/protocol-text.js'


def gen_protocol_text():
    """把 docs/agent-usage-protocol.md 生成 content/protocol-text.js（单一事实源，勿手改产物）。

    生成物形如： var RETRIEVAL_PROTOCOL_MD = "……";
    bootstrap.js 用 loadSubScript 载入，/retrieval/protocol 接口返回。
    """
    src = os.path.join(ROOT, PROTOCOL_SRC.replace('/', os.sep))
    out = os.path.join(ROOT, PROTOCOL_OUT.replace('/', os.sep))
    if not os.path.exists(src):
        raise SystemExit('缺少协议源文件：' + PROTOCOL_SRC)
    md = open(src, encoding='utf-8').read()
    js = ('// 由 build.py 从 ' + PROTOCOL_SRC + ' 自动生成，请勿手改。\n'
          'var RETRIEVAL_PROTOCOL_MD = ' + json.dumps(md, ensure_ascii=False) + ';\n')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(js)
    print('已生成 %s（%d 字符）' % (PROTOCOL_OUT, len(md)))


def main():
    gen_protocol_text()
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
