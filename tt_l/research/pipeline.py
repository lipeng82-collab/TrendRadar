"""Discover -> full text -> semantic screening -> evidence research -> selective delivery."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.parse import urlsplit

import requests
import yaml

from tt_l.topic_cards import canonical_url, normalize_title
from .model import Model, classify, validate_research
from .search import search
from .store import Store, TZ, digest, now
from .web import PublicHTTP, document, index_rows, feed_rows


def discover(http, store, settings, original):
    counts = {}
    sources = list(settings['sources'])
    if original.get('rss', {}).get('enabled'):
        sources += [dict(f, kind='rss') for f in original['rss']['feeds'] if f.get('enabled', True)]
    for source in sources:
        source_id = source['id']
        try:
            rows = []
            for url in source.get('pages', [source.get('url')]):
                raw, _, final = http.get(url)
                if source['kind'] == 'rss':
                    rows += feed_rows(raw, source_id)
                else:
                    rows += index_rows(raw, final, source['link_pattern'], source_id)
            for row in rows:
                store.add(row)
            if not rows:
                raise ValueError('zero_rows_check_adapter')
            counts[source_id] = len(rows)
            store.event(source_id, 'collection', 'success', 'all_discovered_links_queued', count=len(rows))
        except Exception as exc:
            counts[source_id] = 0
            store.event(source_id, 'collection', 'failed', type(exc).__name__)
    # Raw heat-list input; never goes through TrendRadar's title filter or report limit.
    if original.get('platforms', {}).get('enabled'):
        base = original['platforms'].get('api_url') or 'https://newsnow.busiyi.world/api/s'
        for platform in original['platforms']['sources']:
            pid = platform['id']
            try:
                raw, _, _ = http.get(base+'?'+urlencode({'id': pid, 'latest': ''}))
                payload = json.loads(raw)
                if payload.get('status') not in ('success', 'cache'):
                    raise ValueError('newsnow_status')
                rows = payload.get('items', [])
                valid = 0
                for row in rows:
                    url = canonical_url(row.get('url', '') or row.get('mobileUrl', ''))
                    expected = platform.get('expected_domain')
                    host = urlsplit(url).hostname or ''
                    if expected and host != expected and not host.endswith('.'+expected):
                        store.event(pid, 'collection', 'rejected', 'unexpected_domain')
                        continue
                    if url:
                        store.add({'url': url, 'title': row.get('title', ''), 'source': pid})
                        valid += 1
                counts[pid] = valid
                store.event(pid, 'collection', 'success' if valid else 'failed', 'raw_hotlist', count=valid)
            except Exception as exc:
                counts[pid] = 0
                store.event(pid, 'collection', 'failed', type(exc).__name__)
    return counts


def fair_queue(rows):
    groups = defaultdict(deque)
    for row in rows:
        groups[row['source']].append(row)
    while groups:
        for source in list(groups):
            yield groups[source].popleft()
            if not groups[source]:
                del groups[source]


def defer(store, row, stage, reason):
    attempts = row['attempts']+1
    retry_at = (datetime.now(TZ)+timedelta(hours=min(24, 2**min(attempts-1, 5)))).isoformat()
    store.update(row['id'], attempts=attempts, retry_at=retry_at)
    store.event(row['id'], stage, 'deferred', reason, next_retry=retry_at)


def research(http, model, store, row, doc, classification, settings):
    evidence = [dict(doc, id='s0')]
    searches = {}
    fetched = {doc['url']}
    for role, field in [('support', 'search_query'), ('counter', 'counter_query')]:
        try:
            results = search(http, classification[field], store, settings, row['id'])
            searches[role] = 'success'
            store.event(row['id'], 'research_search', 'success', role,
                        query=classification[field], urls=[r['url'] for r in results])
        except Exception as exc:
            searches[role] = 'failed'
            store.event(row['id'], 'research_search', 'failed', role+':'+type(exc).__name__)
            results = []
        # Direct first-party linked releases/PDFs supplement search; they do not masquerade as independent publishers.
        if role == 'support':
            results += [link for link in doc['links'] if re.search(r'\.pdf(?:\?|$)|presscorner/detail', link['url'])]
        successes = 0
        for result in results[:settings['research_links_per_query']]:
            if result['url'] in fetched:
                continue
            fetched.add(result['url'])
            try:
                cache_key = 'document:'+now()[:10]+':'+result['url']
                extra = store.cached(cache_key)
                if extra is None:
                    extra = document(http, result['url'], result.get('published', ''))
                    store.cache(cache_key, extra)
                extra = dict(extra, id='s'+str(len(evidence)), search_role=role)
                evidence.append(extra)
                store.event(row['id'], 'research_fetch', 'success', role, url=extra['url'], sha256=extra['sha256'])
                successes += 1
                if successes >= settings['research_documents_per_query']:
                    break
            except Exception as exc:
                store.event(row['id'], 'research_fetch', 'failed', type(exc).__name__, url=result['url'])
    # Entire extracted evidence enters synthesis; oversized evidence remains pending rather than silently truncated.
    data = {'classification': classification, 'evidence': evidence,
            'previous_recommendations': [json.loads(r[0]) for r in store.db.execute(
                "SELECT payload FROM deliveries WHERE status='sent' ORDER BY updated DESC LIMIT 30")]}
    if len(json.dumps(data, ensure_ascii=False)) > settings['research_max_chars']:
        return {'status': '深研未完成', 'reason': 'evidence_context_limit', 'searches': searches, 'evidence': evidence}
    try:
        card = model.ask(row['id'], 'research_synthesis', '''用给定全文证据做研究卡，不能臆造证据。
返回字符串 main_title(生活化标题，不夸张),subtitle,counterintuitive,mechanism_chain(明确标为研究假设),
application,benefits_and_risks(谁付费谁得益谁承担风险),risks_pending,recommendation_reason,
originality(与历史推送比较；新闻不等于原创),enter_draft(boolean，仅建议，不是作者批准)。
value_score(0-100，证据25/机制25/读者关联20/反差20/表达10)，novelty_score(0-1)。
没有值得解释的新发现就给低分，不因字段写满而给高分；不要凑推送量。
返回 facts 和 counterevidence 两个数组，每项 {text,refs:["s0:p1"]}。
facts 须引原始材料，counterevidence 须有实际段落支持的相反证据或研究局限，
不得把自己提出的问题写成已经查到的反证。明确监管程序、日期、因果局限和当事方立场。
所有事实句的引文只用输入存在的来源编号:段落编号。''', data, 2300)
        complete, reason = validate_research(card, evidence, searches)
        if complete:
            checks = model.ask(row['id'], 'research_verification', '''逐条核对候选卡 facts 和 counterevidence 的陈述与段落。
仅依据给定全文，返回 checks 数组，依次对应 facts 再 counterevidence，
每项 {index:从0起,verdict:"supported"或"contradicted"或"uncertain",ref:"s0:p1",quote:"原文连续短句"}。
quote 应为所引段落中实际存在的连续文本。反证必须支撑相反解释或具体局限，
不能用正向证据充当反证。不因生成器给了引用就相信陈述；初步认定不能写成最终决定。''',
                {'card': card, 'evidence': evidence}, 1500)
            refs = {f"{e['id']}:{p['id']}": p['text'] for e in evidence for p in e['paragraphs']}
            items = checks.get('checks', [])
            claims = card['facts']+card['counterevidence']
            complete = (len(items) == len(claims) and all(
                isinstance(x, dict) and x.get('index') == i and x.get('verdict') == 'supported' and
                x.get('ref') in claims[i]['refs'] and isinstance(x.get('quote'), str) and
                len(x['quote'].strip()) >= 12 and x['quote'] in refs[x['ref']]
                for i, x in enumerate(items)))
            card['verification'] = checks
            reason = 'paragraph_and_verification_checks_passed' if complete else 'claim_verification_incomplete'
    except Exception as exc:
        card, complete, reason = {}, False, str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    return {'status': '深研完成' if complete else '深研未完成', 'reason': reason,
            'card': card, 'searches': searches, 'evidence': evidence,
            'meaning': '自动证据流程完成；模型推断和引文支持程度仍可复核，非事实认证或发表许可'}


def is_duplicate(store, classification, doc):
    for r in store.db.execute("SELECT payload FROM deliveries WHERE status IN ('sent','sending','unknown')"):
        prev = json.loads(r[0])
        if (prev.get('url') == doc['url'] or prev.get('sha256') == doc['sha256'] or
            prev.get('event_key') == classification.get('event_key')):
            return True
        title = normalize_title(doc.get('title', ''))
        old = normalize_title(prev.get('source_title', ''))
        if title and old and SequenceMatcher(None, title, old).ratio() >= 0.88:
            store.event(digest(doc['url'])[:24], 'clustering', 'possible_duplicate',
                        'similar_title_only_not_suppressed')
    return False


def notification_channel():
    # Reuse one existing channel to avoid cross-channel duplicate noise.
    for name, var in [('wework', 'WEWORK_WEBHOOK_URL'), ('feishu', 'FEISHU_WEBHOOK_URL'),
                      ('dingtalk', 'DINGTALK_WEBHOOK_URL'), ('slack', 'SLACK_WEBHOOK_URL')]:
        if os.getenv(var):
            return name, os.environ[var].split(';')[0]
    if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
        return 'telegram', os.environ['TELEGRAM_BOT_TOKEN'].split(';')[0]
    if os.getenv('NTFY_TOPIC'):
        return 'ntfy', (os.getenv('NTFY_SERVER_URL') or 'https://ntfy.sh').rstrip('/')+'/'+os.environ['NTFY_TOPIC'].split(';')[0]
    return '', ''


def send(channel, endpoint, text, key):
    # Do not log endpoint, tokens, raw responses, or transport exception messages.
    session = requests.Session()
    session.trust_env = False
    headers = {'X-Idempotency-Key': key}
    if channel in ('wework', 'dingtalk'):
        payload = {'msgtype': 'markdown', 'markdown': {'content': text, 'title': 'L叔研究线索'}}
    elif channel == 'feishu':
        payload = {'msg_type': 'text', 'content': {'text': text}}
    elif channel == 'slack':
        payload = {'text': text}
    elif channel == 'telegram':
        endpoint = 'https://api.telegram.org/bot'+endpoint+'/sendMessage'
        payload = {'chat_id': os.environ['TELEGRAM_CHAT_ID'].split(';')[0], 'text': text}
    else:
        payload = None
        if os.getenv('NTFY_TOKEN'):
            headers['Authorization'] = 'Bearer '+os.environ['NTFY_TOKEN'].split(';')[0]
    # No retries: an ambiguous timeout must not create duplicate deliveries.
    r = session.post(endpoint, json=payload, data=text.encode('utf-8') if payload is None else None,
                     headers=headers, timeout=20, allow_redirects=False)
    r.raise_for_status()
    if channel in ('wework', 'dingtalk') and r.json().get('errcode') != 0:
        raise ValueError('channel_rejected')
    if channel == 'feishu' and r.json().get('code', r.json().get('StatusCode', -1)) != 0:
        raise ValueError('channel_rejected')
    if channel == 'telegram' and not r.json().get('ok'):
        raise ValueError('channel_rejected')


def push_text(card, url, core):
    # Short mobile notification; complete cards and paragraph evidence stay in reports/state.
    return '\n'.join(['thinking tank L叔｜'+core+'｜深研完成',
        card['main_title'][:70], card['subtitle'][:100],
        '反常识：'+card['counterintuitive'][:140],
        '机制假设：'+card['mechanism_chain'][:180],
        '反证/局限：'+'；'.join(x['text'] for x in card['counterevidence'])[:150],
        '待核：'+card['risks_pending'][:130],
        '推荐：'+card['recommendation_reason'][:100],
        '进入初稿建议：'+('是，待作者决定' if card['enter_draft'] else '否'), url])


def deliver(store, settings, notify):
    channel, endpoint = notification_channel()
    rows = store.db.execute("SELECT * FROM items WHERE status='ready'").fetchall()
    rows = sorted(rows, key=lambda r: json.loads(r['classification'])['score'], reverse=True)
    attempted = store.db.execute("SELECT count(*) FROM deliveries WHERE day=? AND key NOT LIKE 'alert:%'",
                                (now()[:10],)).fetchone()[0]
    result = []
    selected_events, selected_hashes = set(), set()
    for row in rows:
        doc, c, r = (json.loads(row[k]) for k in ('document', 'classification', 'research'))
        if is_duplicate(store, c, doc):
            store.update(row['id'], status='duplicate')
            store.event(row['id'], 'push', 'suppressed', 'already_delivered_event_or_content')
            continue
        if c['event_key'] in selected_events or doc['sha256'] in selected_hashes:
            store.event(row['id'], 'push', 'clustered', 'same_event_in_current_digest')
            continue
        if attempted+len(result) >= settings['max_push_per_day']:
            store.event(row['id'], 'push', 'deferred', 'daily_delivery_limit')
            continue
        result.append({'id': row['id'], 'url': row['url'], 'core': c['core'], **r['card']})
        selected_events.add(c['event_key'])
        selected_hashes.add(doc['sha256'])
        if not notify or not channel:
            store.event(row['id'], 'push', 'dry_run' if not notify else 'unconfigured', 'no_delivery_receipt_written')
            continue
        payload = {'url': row['url'], 'sha256': doc['sha256'], 'event_key': c['event_key'],
                   'source_title': doc['title'], 'title': r['card']['main_title']}
        key = 'card:'+digest(c['event_key'])[:24]
        store.receipt(key, channel, 'sending', payload)
        try:
            send(channel, endpoint, push_text(r['card'], row['url'], c['core']), key)
            store.receipt(key, channel, 'sent', payload)
            store.update(row['id'], status='sent')
            store.event(row['id'], 'push', 'sent', channel)
        except Exception as exc:
            store.receipt(key, channel, 'unknown', payload)
            store.update(row['id'], status='delivery_unknown')
            store.event(row['id'], 'push', 'unknown', type(exc).__name__)
    return result


def run(settings, original, state, output, notify=False, http=None, model=None, skip_discovery=False):
    store = Store(state)
    http = http or PublicHTTP(timeout=settings['http_timeout'], interval=settings['http_interval_seconds'])
    model = model or Model(store, settings, original['ai'])
    started = now()
    counts = {} if skip_discovery else discover(http, store, settings, original)
    rows = store.db.execute("SELECT * FROM items WHERE status IN ('pending','screened') AND retry_at<=? ORDER BY attempts,discovered,id", (now(),)).fetchall()
    researched = 0
    for i, row in enumerate(fair_queue(rows)):
        if i >= settings['max_items_per_run']:
            break
        key = row['id']
        stage = 'fetch'
        try:
            doc = json.loads(row['document']) if row['document'] else document(http, row['url'], row['published'])
            store.update(key, document=doc)
            store.event(key, 'fetch', 'extracted', doc['coverage'], sha256=doc['sha256'],
                        url=doc['url'], published=doc['published'], paragraphs=len(doc['paragraphs']))
            stage = 'classification'
            c = json.loads(row['classification']) if row['classification'] else classify(model, key, doc, settings)
            store.update(key, classification=c, status='screened' if c['relevant'] else 'rejected')
            store.event(key, stage, 'relevant' if c['relevant'] else 'rejected', c['reason'],
                        core=c['core'], score=c['score'], chunks=c['completed_chunks'])
            if not c['relevant']:
                continue
            if c['score'] < settings['research_threshold']:
                store.update(key, status='observation')
                store.event(key, 'research', 'not_started', 'below_fulltext_research_threshold')
                continue
            if researched >= settings['max_research_per_run']:
                store.event(key, 'research', 'deferred', 'research_run_limit')
                continue
            stage = 'research'
            researched += 1
            r = research(http, model, store, row, doc, c, settings)
            complete = r['status'] == '深研完成'
            worthwhile = complete and r['card']['value_score'] >= settings['min_value_score'] and r['card']['novelty_score'] >= settings['min_novelty_score']
            store.update(key, research=r, status=('ready' if worthwhile else 'observation') if complete else 'screened')
            store.event(key, stage, r['status'], r['reason'])
            if complete and not worthwhile:
                store.event(key, 'ranking', 'observation', 'below_value_or_novelty_threshold', value=r['card']['value_score'], novelty=r['card']['novelty_score'])
            if r['status'] != '深研完成':
                defer(store, row, stage, r['reason'])
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (RuntimeError, ValueError, PermissionError)) else type(exc).__name__
            defer(store, row, stage, reason)
            if 'daily_budget_exhausted' in reason or 'model_key_missing' in reason:
                break
    cards = deliver(store, settings, notify)
    # Anomaly alert is aggregated once per Beijing day, not one alert per failed article.
    failures = store.db.execute("SELECT stage,status,count(*) n FROM events WHERE at>=? AND status IN ('failed','deferred','unknown','unconfigured') GROUP BY stage,status", (started,)).fetchall()
    pending = store.db.execute("SELECT status,count(*) n FROM items GROUP BY status").fetchall()
    daily = store.db.execute('SELECT * FROM budgets WHERE day=?', (now()[:10],)).fetchone()
    summary = {'started': started, 'finished': now(), 'sources': counts,
               'queues': [dict(r) for r in pending], 'failures': [dict(r) for r in failures],
               'budget_reserved_ceiling': dict(daily) if daily else {}, 'cards': cards,
            'mode': 'notify' if notify else 'dry_run', 'deep_research_backend': 'direct_fulltext+brave_or_gdelt_api+model',
               'plugin_connected': False, 'history_scope': 'since_initialization_legacy_push_history_not_imported',
               'health': 'degraded' if failures else 'ok'}
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# thinking tank L叔｜全文研究候选', '', '深研完成表示自动证据门禁通过，不是事实认证或发表许可。', '']
    for card in cards:
        lines += ['## '+card['main_title'], card['subtitle'], '', f"核心：{card['core']}；深研完成", card['url']]
        lines += [f"- {k}：{v}" for k, v in card.items() if k not in {'id','main_title','subtitle','url','core'}]
        lines += ['']
    (out/'cards.md').write_text('\n'.join(lines), encoding='utf-8')
    events = store.db.execute('SELECT * FROM events WHERE at>=?', (started,)).fetchall()
    (out/'audit.jsonl').write_text(''.join(json.dumps(dict(r), ensure_ascii=False)+'\n' for r in events), encoding='utf-8')
    channel, endpoint = notification_channel()
    alert_key = 'alert:'+now()[:10]
    if failures and notify and channel and not store.db.execute('SELECT 1 FROM deliveries WHERE key=?', (alert_key,)).fetchone():
        store.receipt(alert_key, channel, 'sending', {'anomaly': True})
        try:
            send(channel, endpoint, 'L叔采集/研究异常（每日汇总）\n'+json.dumps(summary['failures'], ensure_ascii=False)+
                 '\n请查看 GitHub Actions summary/artifact；缺少全文或深研不等于没有选题。', alert_key)
            store.receipt(alert_key, channel, 'sent', {'anomaly': True})
        except Exception as exc:
            store.receipt(alert_key, channel, 'unknown', {'anomaly': True, 'error': type(exc).__name__})
    store.checkpoint()
    store.db.close()
    return summary
