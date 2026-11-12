"""One bounded public fetch; parent owns timeout and persistent state."""
import json
import sys
from .web import PublicHTTP,document

if __name__=='__main__':
    try:
        doc=document(PublicHTTP(timeout=12),sys.argv[1],sys.argv[2] if len(sys.argv)>2 else '')
        print(json.dumps({'ok':True,'document':doc},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__},ensure_ascii=False))
