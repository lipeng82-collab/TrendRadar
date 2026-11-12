from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from .pipeline import run
from .web import PublicHTTP, document, feed_rows, index_rows
from .search import search


def main():
    p = argparse.ArgumentParser(description='thinking tank L叔 全文公开线索研究')
    p.add_argument('--config', type=Path, default=Path('config/tt_l_research.yaml'))
    p.add_argument('--state', type=Path, default=Path('work/tt_l_state/research.db'))
    p.add_argument('--output', type=Path, default=Path('work/tt_l_reports'))
    p.add_argument('--notify', action='store_true', help='发送已有渠道；默认只生成报告')
    p.add_argument('--skip-discovery', action='store_true', help='处理持久队列，不重新采集')
    p.add_argument('--probe', action='store_true', help='只验证来源与漏报原文，不调用模型、不推送')
    a = p.parse_args()
    settings = yaml.safe_load(a.config.read_text(encoding='utf-8'))
    original = yaml.safe_load(Path('config/config.yaml').read_text(encoding='utf-8'))
    if not original.get('tt_l_research', {}).get('enabled', True) and not a.probe:
        print('Research pipeline disabled by config/config.yaml')
        return 0
    if settings.get('profile') != 'personal':
        p.error('This pipeline only accepts the personal public-source profile')
    if a.probe:
        http, results = PublicHTTP(timeout=10), []
        for s in settings['sources']:
            try:
                url = s.get('url') or s['pages'][0]
                raw, _, final = http.get(url)
                rows = feed_rows(raw, s['id']) if s['kind'] == 'rss' else index_rows(raw, final, s['link_pattern'], s['id'])
                results.append({'source': s['id'], 'status': 'success' if rows else 'zero_rows', 'count': len(rows)})
            except Exception as exc:
                results.append({'source': s['id'], 'status': 'failed', 'error': type(exc).__name__})
        url = 'https://digital-strategy.ec.europa.eu/en/news/commission-preliminarily-finds-tiktoks-you-feed-breach-digital-services-act-harmful-rabbit-holes'
        try:
            doc = document(http, url, '2026-11-06')
            results.append({'case': 'TikTok 2026-11-06', 'status': doc['status'], 'paragraphs': len(doc['paragraphs']), 'sha256': doc['sha256']})
        except Exception as exc:
            results.append({'case': 'TikTok 2026-11-06', 'status': 'failed', 'error': type(exc).__name__})
        try:
            links = search(http,'TikTok rabbit')
            results.append({'source':'evidence-search','status':'success','count':len(links)})
        except Exception as exc:
            results.append({'source':'evidence-search','status':'failed','error':str(exc)})
        a.output.mkdir(parents=True, exist_ok=True)
        (a.output/'probe.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(results, ensure_ascii=False))
        return 1 if any(r['status'] not in ('success', 'extracted') for r in results) else 0
    try:
        result = run(settings, original, a.state, a.output, a.notify, skip_discovery=a.skip_discovery)
        print(json.dumps({k: result[k] for k in ('mode', 'queues', 'budget_reserved_ceiling')}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print('Research pipeline failed: '+type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
