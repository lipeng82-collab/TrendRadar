"""Auditable handoff for Codex chat/heartbeat research without separate API keys.

The agent reads every full-text packet and returns decisions/evidence explicitly.
No fake model responses, no API credentials, no claimed external delivery.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import yaml

from tt_l.topic_cards import canonical_url
from .model import CORES, validate_research
from .pipeline import discover, fair_queue, defer
from .store import Store, TZ, digest, now
from .web import PublicHTTP, document


def bounded_document(url,published='',timeout=45):
    try:
        result=subprocess.run([sys.executable,'-X','utf8','-m','tt_l.research.fetch_worker',url,published],
                              capture_output=True,text=True,encoding='utf-8',timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('document_wall_clock_timeout') from None
    if result.returncode!=0:
        raise RuntimeError('document_worker_failed')
    payload=json.loads(result.stdout)
    if not payload.get('ok'):
        raise RuntimeError('document_fetch_'+payload.get('error','failed'))
    return payload['document']


def initialize(store):
    store.db.executescript('''
    CREATE TABLE IF NOT EXISTS app_runs(id TEXT PRIMARY KEY,started TEXT,day TEXT,documents INTEGER DEFAULT 0,searches INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS app_reports(event_key TEXT PRIMARY KEY,item TEXT,url TEXT,sha256 TEXT,day TEXT,path TEXT,card TEXT);
    ''')


def prepare(state, out, limit=12, collect=True):
    if not 1 <= limit <= 12:
        raise ValueError('per_run_limit_1_to_12')
    store = Store(state)
    initialize(store)
    run_id = datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
    day = now()[:10]
    used = store.db.execute('SELECT COALESCE(sum(documents),0) FROM app_runs WHERE day=?',(day,)).fetchone()[0]
    available = max(0,min(limit,30-used))
    with store.db:
        store.db.execute('INSERT INTO app_runs(id,started,day) VALUES(?,?,?)',(run_id,now(),day))
    settings=yaml.safe_load(Path('config/tt_l_research.yaml').read_text(encoding='utf-8'))
    original=yaml.safe_load(Path('config/config.yaml').read_text(encoding='utf-8'))
    http=PublicHTTP(timeout=12,interval=1)
    counts=discover(http,store,settings,original) if collect else {}
    rows=store.db.execute("SELECT * FROM items WHERE status='pending' AND retry_at<=? ORDER BY attempts,discovered,id",(now(),)).fetchall()
    destination=Path(out)/run_id
    destination.mkdir(parents=True,exist_ok=True)
    packets=[]
    attempted=0
    for row in fair_queue(rows):
        if attempted >= available:
            break
        attempted+=1
        try:
            doc=json.loads(row['document']) if row['document'] else bounded_document(row['url'],row['published'])
            store.update(row['id'],document=doc)
            # Preserve whole document; never silently excerpt or classify only the beginning.
            size=sum(len(p['text']) for p in doc['paragraphs'])
            if size>60000:
                defer(store,row,'classification','full_document_requires_separate_long_read')
                continue
            path=destination/(row['id']+'.json')
            packet={'id':row['id'],'source':row['source'],'title':row['title'],'document':doc,
                    'required_paragraphs':[p['id'] for p in doc['paragraphs']],
                    'backend':'Codex chat/heartbeat; not an independent API deployment'}
            path.write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
            packets.append({'id':row['id'],'path':str(path.resolve()),'characters':size,
                            'paragraphs':len(doc['paragraphs']),'source':row['source']})
            store.event(row['id'],'fetch','extracted','full_text_packet_ready',sha256=doc['sha256'],paragraphs=len(doc['paragraphs']))
        except Exception as exc:
            defer(store,row,'fetch',str(exc) if isinstance(exc,(ValueError,PermissionError,RuntimeError)) else type(exc).__name__)
    with store.db:
        store.db.execute('UPDATE app_runs SET documents=? WHERE id=?',(attempted,run_id))
    manifest={'run_id':run_id,'backend':'Codex chat/heartbeat','started':now(),'sources':counts,
              'attempted_documents':attempted,'packets':packets,'daily_document_cap':30,
              'model_tokens':None,'usd_cost':None,'usage_status':'not_exposed_by_chat_backend',
              'queue':[dict(r) for r in store.db.execute('SELECT status,count(*) count FROM items GROUP BY status')]}
    manifest_path=destination/'manifest.json'
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    store.checkpoint()
    store.db.close()
    return manifest_path,manifest


def decisions(state, path):
    store=Store(state)
    try:
        return _decisions(store,path)
    finally:
        store.db.close()


def _decisions(store,path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    for decision in data:
        row=store.db.execute('SELECT * FROM items WHERE id=?',(decision['id'],)).fetchone()
        if row is None or not row['document']:
            raise ValueError('unknown_or_unfetched_item')
        doc=json.loads(row['document'])
        if decision.get('sha256')!=doc['sha256'] or decision.get('read_paragraphs')!=[p['id'] for p in doc['paragraphs']]:
            raise ValueError('fulltext_read_coverage_or_version_mismatch')
        if type(decision.get('relevant')) is not bool or not isinstance(decision.get('reason'),str):
            raise ValueError('invalid_decision')
        if decision['relevant'] and decision.get('core') not in CORES:
            raise ValueError('invalid_core')
        if type(decision.get('score')) not in (int,float) or not 0<=decision['score']<=1:
            raise ValueError('invalid_score')
        store.update(row['id'],classification=decision,status='screened' if decision['relevant'] else 'rejected')
        store.event(row['id'],'classification','relevant' if decision['relevant'] else 'rejected',decision['reason'],
                    backend='Codex chat/heartbeat',coverage='all_paragraphs',usage_status='not_exposed')
    store.checkpoint()
    return len(data)


def publish(state, research_path, output, mark_reported=True):
    store=Store(state)
    try:
        return _publish(store,research_path,output,mark_reported)
    finally:
        store.db.close()


def _publish(store,research_path,output,mark_reported=True):
    initialize(store)
    records=json.loads(Path(research_path).read_text(encoding='utf-8'))
    day=now()[:10]
    count=store.db.execute('SELECT count(*) FROM app_reports WHERE day=?',(day,)).fetchone()[0]
    accepted=[]
    for record in records:
        row=store.db.execute('SELECT * FROM items WHERE id=?',(record['id'],)).fetchone()
        if row is None or not row['classification']:
            raise ValueError('classify_before_research')
        c=json.loads(row['classification'])
        evidence=record['evidence']
        primary=json.loads(row['document'])
        if not evidence or evidence[0]['sha256']!=primary['sha256'] or evidence[0]['url']!=primary['url']:
            raise ValueError('primary_document_changed')
        for e in evidence:
            if not canonical_url(e['url']) or e['sha256']!=digest('\n'.join(p['text'] for p in e['paragraphs'])):
                raise ValueError('invalid_source_snapshot')
        searches=record['searches']
        trace=record.get('search_trace',[])
        if not all(any(t.get('role')==role and t.get('query') and t.get('result_urls') for t in trace) for role in ('support','counter')):
            raise ValueError('missing_real_search_trace')
        if len(trace)>16:
            raise ValueError('search_budget_exceeded')
        complete,reason=validate_research(record['card'],evidence,searches)
        refs={f"{e['id']}:{p['id']}":p['text'] for e in evidence for p in e['paragraphs']}
        claims=record['card']['facts']+record['card']['counterevidence']
        checks=record.get('verification',[])
        if complete:
            complete=(len(checks)==len(claims) and all(
                x.get('index')==i and x.get('verdict')=='supported' and x.get('ref') in claims[i]['refs'] and
                isinstance(x.get('quote'),str) and len(x['quote'].strip())>=12 and x['quote'] in refs[x['ref']]
                for i,x in enumerate(checks)))
            if not complete:
                reason='quote_or_claim_verification_failed'
        research=dict(record,status='深研完成' if complete else '深研未完成',reason=reason,
                      backend='Codex + web search + direct fulltext snapshots',usage_status='not_exposed')
        store.update(row['id'],research=research,status='observation')
        store.event(row['id'],'research',research['status'],reason,backend=research['backend'])
        key=c.get('event_key') or row['url']
        duplicate=store.db.execute('SELECT 1 FROM app_reports WHERE event_key=? OR url=? OR sha256=?',
                                   (key,row['url'],evidence[0]['sha256'])).fetchone()
        card=record['card']
        if not complete or card['value_score']<80 or card['novelty_score']<0.65 or duplicate or count+len(accepted)>=3:
            store.event(row['id'],'push','suppressed','incomplete_low_value_duplicate_or_daily_cap')
            continue
        accepted.append(dict(record,id=row['id'],url=row['url'],core=c['core'],event_key=key))
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    export_cards=[]
    for r in accepted:
        public_record={k:v for k,v in r.items() if k!='evidence'}
        public_record['evidence']=[{k:v for k,v in e.items() if k not in ('paragraphs','links')} |
            {'paragraphs':[{'id':p['id'],'page':p.get('page'),'characters':len(p['text'])} for p in e['paragraphs']]} for e in r['evidence']]
        export_cards.append(public_record)
    report={'date':day,'backend':'Codex research with source snapshots','cards':export_cards,
            'model_tokens':None,'usd_cost':None,'delivery':'local_report_ready; no external channel acknowledgement'}
    json_file=output/'选题卡.json'
    md_file=output/'今日选题卡.md'
    html_file=output/'今日选题卡.html'
    if not accepted and json_file.exists():
        store.checkpoint()
        return {'cards':0,'existing_report_preserved':True,'html':str(html_file.resolve()),'markdown':str(md_file.resolve())}
    json_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f'# thinking tank L叔｜{day} 全文研究选题','',
           '依据公开原文、交叉检索和反证形成；模型研究判断供作者决定，未自动写稿或发布。',
           '运行方式：Codex研究；当前聊天后端不提供可核验token账单，费用不填0。','']
    blocks=[]
    for n,r in enumerate(accepted,1):
        card=r['card']
        lines += [f"## {n}. {card['main_title']}",card['subtitle'],'',f"归属：{r['core']}｜深研完成（证据流程）｜价值 {card['value_score']}/100",'']
        for label,field in [('核心反常识','counterintuitive'),('机制假设','mechanism_chain'),('生活应用','application'),
                            ('利益与风险','benefits_and_risks'),('风险/待核','risks_pending'),('推荐理由','recommendation_reason'),('原创性判断','originality')]:
            lines += [f'**{label}**：{card[field]}','']
        lines += ['**事实与证据**']+[f"- {x['text']}〔{', '.join(x['refs'])}〕" for x in card['facts']]
        lines += ['','**反证与限制**']+[f"- {x['text']}〔{', '.join(x['refs'])}〕" for x in card['counterevidence']]
        lines += ['','**原始来源**']+[f"- [{e.get('title') or e['id']}]({e['url']})；源日期 {e.get('published') or '待核'}；证据 {e['id']}；{len(e['paragraphs'])}段" for e in r['evidence']]
        lines += ['',f"**是否进入初稿**：{'建议进入，待作者决定' if card['enter_draft'] else '暂不进入'}",'']
        source_links=''.join(f'<li><a href="{escape(e["url"],quote=True)}" target="_blank" rel="noreferrer">{escape(e.get("title") or e["id"])}</a> · {escape(e.get("published") or "日期待核")} · {e["id"]}</li>' for e in r['evidence'])
        fields=''.join(f'<section><h3>{label}</h3><p>{escape(card[field])}</p></section>' for label,field in [
            ('为什么值得看','counterintuitive'),('机制假设','mechanism_chain'),('谁受益、谁承担风险','benefits_and_risks'),('应用','application'),('风险与待核','risks_pending')])
        counters=''.join('<li>'+escape(x['text'])+'</li>' for x in card['counterevidence'])
        blocks.append(f'<article><div class="badge">{escape(r["core"])} · 深研完成（证据流程）</div><h2>{escape(card["main_title"])}</h2><p class="subtitle">{escape(card["subtitle"])}</p>{fields}<section><h3>反证与限制</h3><ul>{counters}</ul></section><details><summary>查看原始证据与日期</summary><ul>{source_links}</ul></details><footer>价值 {card["value_score"]}/100 · {"建议进入初稿，待作者决定" if card["enter_draft"] else "暂不进入初稿"}</footer></article>')
    md_file.write_text('\n'.join(lines),encoding='utf-8')
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>L叔 · 全文研究选题</title><style>body{margin:0;background:#f3f2ee;color:#182625;font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif}main{max-width:960px;margin:auto;padding:40px 24px}h1{font-size:34px;line-height:1.4}.eyebrow,.badge{color:#16635a;font-weight:650;font-size:14px}article{background:#fff;padding:30px;margin:28px 0;border:1px solid #dce2de;border-radius:12px}h2{font-size:26px;line-height:1.45}h3{font-size:15px;margin:20px 0 4px;color:#48615c}p{margin:4px 0}.subtitle{color:#53615e}details{padding:16px 0}a{color:#096b60;overflow-wrap:anywhere}footer{border-top:1px solid #e7ebe8;padding-top:16px;color:#687571;font-size:14px}.note{font-size:14px;color:#687571}@media(max-width:600px){main{padding:24px 14px}article{padding:22px}h1{font-size:28px}h2{font-size:23px}}</style><main>'''
    html+=f'<div class="eyebrow">thinking tank L叔 / 全文研究</div><h1>{day}，值得解释的变化</h1><p class="note">事实 → 机制假设 → 反证 → 应用。只用公开材料；报告可直接阅读，写作由作者决定。</p>'
    html+=''.join(blocks) or '<p>本轮没有通过完整证据门槛的新选题，未凑数量。</p>'
    html+='<p class="note">运行：Codex研究与网页/PDF证据。未接入独立模型API，token与费用账单未暴露；未向外部渠道发送。</p></main></html>'
    html_file.write_text(html,encoding='utf-8')
    if mark_reported:
        for r in accepted:
            with store.db:
                store.db.execute('INSERT INTO app_reports VALUES(?,?,?,?,?,?,?)',
                    (r['event_key'],r['id'],r['url'],r['evidence'][0]['sha256'],day,str(html_file.resolve()),json.dumps(r['card'],ensure_ascii=False)))
            store.update(r['id'],status='app_reported')
            store.event(r['id'],'push','artifact_ready','local_report_written_not_external_delivery',path=str(html_file.resolve()))
    store.checkpoint()
    return {'cards':len(accepted),'html':str(html_file.resolve()),'markdown':str(md_file.resolve())}


def main():
    p=argparse.ArgumentParser(description='Codex全文研究交接与审计，不需要独立API密钥')
    p.add_argument('--state',type=Path,default=Path('work/tt_l_state/codex.db'))
    commands=p.add_subparsers(dest='command',required=True)
    q=commands.add_parser('queue')
    q.add_argument('--url',required=True)
    q.add_argument('--title',default='公开线索')
    q.add_argument('--source',default='explicit_public_url')
    q.add_argument('--published',default='')
    a=commands.add_parser('prepare')
    a.add_argument('--out',type=Path,default=Path('work/codex_packets'))
    a.add_argument('--limit',type=int,default=12)
    a.add_argument('--skip-discovery',action='store_true')
    d=commands.add_parser('decisions')
    d.add_argument('--file',type=Path,required=True)
    r=commands.add_parser('publish')
    r.add_argument('--file',type=Path,required=True)
    r.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if args.command=='queue':
        u=canonical_url(args.url)
        if not u: p.error('公开安全URL必需')
        s=Store(args.state)
        print(s.add({'url':u,'title':args.title,'source':args.source,'published':args.published}))
        s.db.close()
    elif args.command=='prepare':
        path,manifest=prepare(args.state,args.out,args.limit,not args.skip_discovery)
        print(json.dumps({'manifest':str(path.resolve()),**manifest},ensure_ascii=False))
    elif args.command=='decisions':
        print(json.dumps({'recorded':decisions(args.state,args.file)}))
    else:
        print(json.dumps(publish(args.state,args.file,args.out),ensure_ascii=False))


if __name__=='__main__': main()
