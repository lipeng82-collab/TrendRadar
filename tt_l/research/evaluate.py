"""16-case evaluation: separate title baseline, contract replay and actual model results."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from tt_l.topic_cards import rank_relevance
from .model import Model, classify
from .store import Store, digest, now

FIXTURES = Path(__file__).parents[1]/'fixtures'/'fulltext_cases.json'


def metrics(labels, predictions):
    tp = sum(y and p for y, p in zip(labels, predictions))
    fp = sum(not y and p for y, p in zip(labels, predictions))
    fn = sum(y and not p for y, p in zip(labels, predictions))
    tn = sum(not y and not p for y, p in zip(labels, predictions))
    return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
            'recall': tp/(tp+fn) if tp+fn else None,
            'false_positive_rate': fp/(fp+tn) if fp+tn else None,
            'precision': tp/(tp+fp) if tp+fp else None}


def make_doc(case):
    return {'url': case['url'], 'title': case['title'], 'published': case.get('published', ''),
            'paragraphs': [{'id': 'p1', 'text': case['body']}], 'links': [],
            'sha256': digest(case['body']), 'status': 'extracted', 'coverage': 'synthetic_fixture'}


def evaluate(live=False, state='work/tt_l_eval.db'):
    cases = json.loads(FIXTURES.read_text(encoding='utf-8'))
    labels = [c['expected_relevant'] for c in cases]
    baseline = [rank_relevance(c, 'personal')[0] >= 35 for c in cases]
    result = {'sample_count': len(cases), 'sample_type': 'synthetic mechanism cases; TikTok case is a paraphrase of official source, not a live crawl',
              'legacy_title_keyword_baseline': metrics(labels, baseline),
              'new_model_metrics': None, 'actual_model_tokens': 0, 'actual_model_calls': 0,
              'new_model_status': 'NOT_MEASURED_no_model_called',
              'daily_push_configured_cap': 3, 'actual_pushes_in_evaluation': 0,
              'discovery_retention_contract': 'All safe URLs enter queue before any model screening; covered by unit tests'}
    if live:
        started = now()
        settings = yaml.safe_load(Path('config/tt_l_research.yaml').read_text(encoding='utf-8'))
        original = yaml.safe_load(Path('config/config.yaml').read_text(encoding='utf-8'))
        store = Store(state)
        model = Model(store, settings, original['ai'])
        predictions = []
        for case in cases:
            # Golden labels are never passed to the model.
            output = classify(model, case['id'], make_doc(case), settings)
            predictions.append({'id': case['id'], **output})
        result['predictions'] = predictions
        result['new_model_metrics'] = metrics(labels, [p['relevant'] for p in predictions])
        result['positive_core_accuracy'] = sum(p['core']==c['expected_core'] for p,c in zip(predictions,cases) if c['expected_relevant'])/sum(labels)
        result['new_model_status'] = 'LIVE_MODEL_synthetic_cases_not_production_recall'
        usage = [json.loads(r[0]).get('usage', {}) for r in store.db.execute("SELECT data FROM events WHERE status='response' AND at>=?", (started,))]
        result['actual_model_calls'] = len(usage)
        result['actual_model_tokens'] = sum(r.get('total_tokens', 0) for r in usage)
        result['reserved_cost_ceiling'] = [dict(r) for r in store.db.execute('SELECT * FROM budgets')]
        store.checkpoint()
        store.db.close()
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true', help='Requires AI_API_KEY; measured model evaluation uses its own local state')
    p.add_argument('--output', type=Path, default=Path('work/regression.json'))
    p.add_argument('--state', default='work/tt_l_eval.db')
    a = p.parse_args()
    result = evaluate(a.live,a.state)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
