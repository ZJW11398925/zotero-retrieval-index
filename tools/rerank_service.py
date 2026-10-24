# -*- coding: utf-8 -*-
"""本地 GPU 重排服务（bge-reranker-base, PyTorch fp16 CUDA）

用法：
  python tools/rerank_service.py            # 默认 127.0.0.1:8899
  python tools/rerank_service.py --port 8899 --max-len 512

接口：
  GET  /health                      → {ok, device, model, maxLen}
  POST /rerank  {"q": "...", "passages": ["...", ...]}  → {"scores": [..]}
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

MODEL = os.environ.get('RERANK_MODEL_DIR') or r'D:\zotero\opt_eval\t14\rr_model'
_state = {'model': None, 'tok': None, 'device': None, 'max_len': 512}
_lock = threading.Lock()


def load_model(max_len=512):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    if _state['model'] is not None:
        return
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(MODEL)
    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForSequenceClassification.from_pretrained(MODEL, torch_dtype=dtype)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device).eval()
    _state.update({'model': model, 'tok': tok, 'device': device, 'max_len': max_len})
    print('[rerank] loaded %s on %s in %.1fs (max_len=%d)' % (MODEL, device, time.time() - t0, max_len), flush=True)


def score(q, passages):
    import torch
    load_model(_state['max_len'])
    tok, model = _state['tok'], _state['model']
    enc = tok([q] * len(passages), passages, padding=True, truncation=True,
              max_length=_state['max_len'], return_tensors='pt')
    device = _state['device']
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.inference_mode():
        logits = model(**enc).logits.view(-1).float()
    return logits.cpu().tolist()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/health'):
            load_model(_state['max_len'])
            self._send(200, {'ok': True, 'device': _state['device'], 'model': os.path.basename(MODEL),
                             'maxLen': _state['max_len']})
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n).decode('utf-8'))
            q = str(req.get('q') or '')
            passages = [str(x) for x in (req.get('passages') or [])]
            if not q or not passages:
                self._send(400, {'error': 'q 或 passages 为空'})
                return
            t0 = time.time()
            with _lock:
                scores = score(q, passages)
            self._send(200, {'scores': scores, 'ms': round((time.time() - t0) * 1000)})
        except Exception as e:
            self._send(500, {'error': str(e)[:300]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8898)
    ap.add_argument('--max-len', type=int, default=512)
    args = ap.parse_args()
    _state['max_len'] = args.max_len
    load_model(args.max_len)  # 启动即加载，避免首个请求慢
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print('[rerank] listening on http://%s:%d' % (args.host, args.port), flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
