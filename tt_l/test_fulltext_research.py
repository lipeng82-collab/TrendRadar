"""Fault, full-body coverage, evidence, durable budget and delivery regression tests."""
import copy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from tt_l.research.evaluate import FIXTURES, make_doc, metrics
from tt_l.research.model import Model, chunks, classify, validate_research
from tt_l.research.pipeline import discover, deliver, run
from tt_l.research.store import Store, now
from tt_l.research.web import extract, feed_rows, public_url, PublicHTTP
from tt_l.research.search import search
from tt_l.research.app_bridge import decisions as app_decisions, publish as app_publish, bounded_document
import subprocess

ROOT = Path(__file__).parents[1]


def judgment(relevant=True, score=0.9):
    return dict(relevant=relevant, score=score, core='注意力' if relevant else '无',
                reason='正文机制证据', event_key='TikTok|2026-11-06|rabbit-hole',
                search_query='TikTok rabbit hole findings November 2026',
                counter_query='TikTok response rabbit hole study limitations')


def card():
    result = {k: '合成回归内容，研究假设待核' for k in ['main_title','subtitle','counterintuitive',
        'mechanism_chain','application','benefits_and_risks','risks_pending','recommendation_reason','originality']}
    result.update(enter_draft=False,value_score=90,novelty_score=0.9,
        facts=[{'text':'监管初步认定', 'refs':['s0:p1']}],
        counterevidence=[{'text':'当事方反驳与研究局限', 'refs':['s1:p1']}])
    return result


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'research.db')
        self.addCleanup(self.store.db.close)
        self.settings = yaml.safe_load((ROOT/'config/tt_l_research.yaml').read_text(encoding='utf-8'))
        self.settings['model_interval_seconds'] = 0
        self.case = json.loads(FIXTURES.read_text(encoding='utf-8'))[0]
        self.doc = make_doc(self.case)

    def test_16_safe_cases_are_queued_before_title_judgment(self):
        cases = json.loads(FIXTURES.read_text(encoding='utf-8'))
        self.assertEqual(len(cases), 16)
        self.assertEqual(sum(c['expected_relevant'] for c in cases), 8)
        class HTTP:
            def get(_, url):
                raw = '<rss><channel>'+''.join(f"<item><title>{c['title']}</title><link>{c['url']}</link></item>" for c in cases)+'</channel></rss>'
                return raw.encode(), 'application/rss+xml', url
        s = dict(self.settings, sources=[{'id':'test', 'url':'https://example.org/feed', 'kind':'rss'}])
        counts = discover(HTTP(), self.store, s, {})
        self.assertEqual(counts['test'], 16)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM items').fetchone()[0], 16)

    def test_relevance_hidden_in_last_fulltext_chunk_survives(self):
        self.doc['paragraphs'] = [{'id':'p1','text':'x'*15000}, {'id':'p2','text':'hidden mechanism'}]
        class Fake:
            def ask(_, key, stage, instruction, data, max_tokens):
                return judgment(any(p['id']=='p2' for p in data['paragraphs']))
        c = classify(Fake(), 'one', self.doc, self.settings)
        self.assertTrue(c['relevant'])
        self.assertEqual(c['completed_chunks'], 2)
        self.assertEqual(''.join(p['text'] for g in chunks(self.doc, 10000) for p in g), 'x'*15000+'hidden mechanism')

    def test_incomplete_model_batch_never_rejects(self):
        self.doc['paragraphs'][0]['text'] = 'x'*21000
        class Fake:
            calls=0
            def ask(self, *a, **kw):
                self.calls+=1
                if self.calls==2:
                    raise RuntimeError('daily_budget_exhausted')
                return judgment(False)
        with self.assertRaises(RuntimeError):
            classify(Fake(), 'one', self.doc, self.settings)

    def test_budget_is_durable_across_new_process_connection(self):
        limits = dict(self.settings['budget'], daily_tokens=100)
        self.store.reserve(80, 0.01, limits)
        another=Store(self.root/'research.db')
        try:
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                another.reserve(21, 0.01, limits)
        finally:
            another.db.close()

    def test_timeout_reservation_is_not_refunded(self):
        def fail(**kwargs):
            raise TimeoutError('must never be logged raw')
        model = Model(self.store, self.settings, {'model':'test/model'}, completion=fail)
        with self.assertRaises(RuntimeError):
            model.ask('one','classification','task',{'body':'test'},10)
        row=self.store.db.execute('SELECT * FROM budgets').fetchone()
        self.assertEqual(row['calls'],1)
        self.assertGreater(row['tokens'],10)
        logs=''.join(r[0] for r in self.store.db.execute('SELECT reason FROM events'))
        self.assertNotIn('must never',logs)

    def test_cached_model_result_has_zero_additional_cost(self):
        calls=[]
        def respond(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],usage={'total_tokens':3})
        model=Model(self.store,self.settings,{'model':'test/model'},completion=respond)
        for _ in range(2):
            self.assertTrue(model.ask('one','classification','task',{})['ok'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['num_retries'],0)

    def test_html_evidence_excludes_scripts_and_preserves_paragraphs_date(self):
        html='<html><title>Story</title><main><time datetime="2026-11-06"></time><p>'+'valid sentence '*35+'</p><p>'+'second sentence '*30+'</p><script>EVIL INSTRUCTION</script></main></html>'
        doc=extract(html.encode(),'text/html','https://example.org/story')
        self.assertEqual(doc['published'],'2026-11-06')
        self.assertEqual(len(doc['paragraphs']),2)
        self.assertNotIn('EVIL',str(doc['paragraphs']))

    def test_paywall_and_short_abstract_never_marked_fulltext(self):
        for body in ['<p>Short abstract</p>', '<main><p>'+('a'*500)+'</p><script>{"isAccessibleForFree":false}</script></main>']:
            with self.assertRaises(ValueError):
                extract(body.encode(),'text/html','https://example.org/article')

    def test_atom_and_rdf_discovery(self):
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>One</title><link href="https://example.org/one"/><published>2026-11-06</published></entry></feed>'
        self.assertEqual(feed_rows(atom,'source')[0]['published'],'2026-11-06')
        rdf=b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"><item><title>Two</title><link>https://example.org/two</link></item></rdf:RDF>'
        self.assertEqual(len(feed_rows(rdf,'source')),1)

    def test_search_index_time_is_not_publication_date(self):
        class HTTP:
            interval=1
            def get(_,url):
                return b'{"articles":[{"url":"https://example.org/story","title":"Story","seendate":"20261106T100000Z"}]}','application/json',url
        with patch.dict(os.environ,{},clear=True):
            row=search(HTTP(),'test query')[0]
        self.assertEqual(row['published'],'')
        self.assertEqual(row['indexed_at'],'20261106T100000Z')

    def test_private_and_access_bearing_urls_rejected(self):
        for url in ['https://127.0.0.1/a','https://example.org/a?token=x','file:///a','https://a:b@example.org/a']:
            with self.assertRaises(ValueError):
                public_url(url,resolve=False)

    def test_request_url_preserves_required_trailing_slash(self):
        self.assertEqual(public_url('https://example.org/article/?utm_source=x',resolve=False),
                         'https://example.org/article/')

    def app_record(self):
        key=self.store.add({'url':self.doc['url'],'title':'Synthetic app fixture','source':'test'})
        self.store.update(key,document=self.doc,classification=judgment())
        evidence=self.evidence()
        evidence[0]['url']=self.doc['url']
        body=self.doc['paragraphs'][0]['text']
        record={'id':key,'card':card(),'evidence':evidence,
                'searches':{'support':'success','counter':'success'},
                'search_trace':[{'role':'support','query':'synthetic support','result_urls':[self.doc['url']]},
                                {'role':'counter','query':'synthetic counter','result_urls':[evidence[1]['url']]}],
                'verification':[{'index':0,'verdict':'supported','ref':'s0:p1','quote':body[:20]},
                                {'index':1,'verdict':'supported','ref':'s1:p1','quote':body[:20]}]}
        path=self.root/'app-research.json'
        path.write_text(json.dumps([record],ensure_ascii=False),encoding='utf-8')
        return path,record

    def test_app_requires_all_paragraphs_before_semantic_decision(self):
        key=self.store.add({'url':self.doc['url'],'source':'test'})
        self.store.update(key,document=self.doc)
        d=dict(judgment(),id=key,sha256=self.doc['sha256'],read_paragraphs=[])
        path=self.root/'decision.json'
        path.write_text(json.dumps([d]),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'coverage'):
            app_decisions(self.root/'research.db',path)
        self.assertIsNone(self.store.db.execute('SELECT classification FROM items').fetchone()[0])

    def test_app_replay_preserves_existing_report_and_never_claims_external_delivery(self):
        path,record=self.app_record()
        out=self.root/'report'
        first=app_publish(self.root/'research.db',path,out)
        self.assertEqual(first['cards'],1)
        before=(out/'今日选题卡.html').read_bytes()
        rendered=before.decode('utf-8')
        for required in ['事实与证据','推荐理由','原创性判断','s0:p1','反证与限制']:
            self.assertIn(required,rendered)
        second=app_publish(self.root/'research.db',path,out)
        self.assertEqual(second['cards'],0)
        self.assertTrue(second['existing_report_preserved'])
        self.assertEqual(before,(out/'今日选题卡.html').read_bytes())
        data=json.loads((out/'选题卡.json').read_text(encoding='utf-8'))
        self.assertIsNone(data['model_tokens'])
        self.assertIn('no external',data['delivery'])
        self.assertNotIn('text',data['cards'][0]['evidence'][0]['paragraphs'][0])

    def test_app_cannot_relabel_primary_url(self):
        path,record=self.app_record()
        record['evidence'][0]['url']='https://example.org/forged-primary'
        path.write_text(json.dumps([record],ensure_ascii=False),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'primary_document'):
            app_publish(self.root/'research.db',path,self.root/'report')

    def test_app_rejects_invented_verification_quote(self):
        path,record=self.app_record()
        record['verification'][0]['quote']='This quotation does not exist in the evidence'
        path.write_text(json.dumps([record],ensure_ascii=False),encoding='utf-8')
        result=app_publish(self.root/'research.db',path,self.root/'report')
        self.assertEqual(result['cards'],0)
        self.assertIn('深研未完成',self.store.db.execute('SELECT research FROM items').fetchone()[0])

    def test_public_fetch_worker_has_wall_clock_timeout(self):
        with patch('tt_l.research.app_bridge.subprocess.run',side_effect=subprocess.TimeoutExpired('fetch',45)):
            with self.assertRaisesRegex(RuntimeError,'wall_clock_timeout'):
                bounded_document('https://example.org/paper')

    def test_dns_private_resolution_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('10.1.2.3',443))]):
            with self.assertRaises(ValueError):
                public_url('https://example.org/a')

    def evidence(self):
        return [dict(self.doc,id='s0',url='https://digital-strategy.ec.europa.eu/en/news/a'),
                dict(self.doc,id='s1',url='https://www.tiktok.com/transparency/a')]

    def test_invalid_citations_or_missing_counterevidence_cannot_complete(self):
        c=card()
        self.assertTrue(validate_research(c,self.evidence(),{'support':'success','counter':'success'})[0])
        for broken in [dict(c,counterevidence=[]),dict(c,facts=[{'text':'x','refs':['s0:p999']}])]:
            self.assertFalse(validate_research(broken,self.evidence(),{'support':'success','counter':'success'})[0])
        self.assertFalse(validate_research(c,self.evidence(),{'support':'success','counter':'failed'})[0])

    def test_two_ec_sites_are_one_publisher(self):
        e=self.evidence()
        e[1]['url']='https://ec.europa.eu/presscorner/a'
        self.assertFalse(validate_research(card(),e,{'support':'success','counter':'success'})[0])

    def ready(self, suffix='1'):
        key=self.store.add({'url':'https://example.org/'+suffix,'title':'A','source':'test'})
        self.store.update(key,status='ready',document=self.doc,classification=judgment(),research={'card':card(),'status':'深研完成'})
        return key

    def test_dry_run_does_not_mark_sent(self):
        self.ready()
        self.assertEqual(len(deliver(self.store,self.settings,False)),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],0)

    def test_dry_run_clusters_same_event(self):
        self.ready('1')
        self.ready('2')
        self.assertEqual(len(deliver(self.store,self.settings,False)),1)

    def test_daily_delivery_cap_survives_many_distinct_candidates(self):
        for i in range(5):
            key=self.ready(str(i))
            doc=dict(self.doc,sha256='hash'+str(i),title='Distinct '+str(i),url='https://example.org/'+str(i))
            self.store.update(key,document=doc,classification=dict(judgment(),event_key='event'+str(i)))
        with patch.dict(os.environ,{'WEWORK_WEBHOOK_URL':'https://example.org/webhook'},clear=True),patch('tt_l.research.pipeline.send') as send:
            deliver(self.store,self.settings,True)
            deliver(self.store,self.settings,True)
            self.assertEqual(send.call_count,3)

    def test_complete_pipeline_with_injected_public_evidence_and_receipt(self):
        row={'url':self.case['url'],'source':'ec-digital','title':self.case['title']}
        self.store.add(row)
        doc=dict(self.doc,coverage='synthetic_fixture',published='2026-11-06')
        body=doc['paragraphs'][0]['text']
        class FakeModel:
            def ask(_, key,stage,instruction,data,max_tokens):
                if stage=='classification':
                    return judgment()
                if stage=='research_synthesis':
                    return card()
                return {'checks':[{'index':0,'verdict':'supported','ref':'s0:p1','quote':body[:20]},
                                  {'index':1,'verdict':'supported','ref':'s1:p1','quote':body[:20]}]}
        def fake_document(http,url,published=''):
            return dict(doc,url=url)
        with patch('tt_l.research.pipeline.document',side_effect=fake_document),patch('tt_l.research.pipeline.search',return_value=[{'url':'https://www.tiktok.com/transparency/test'}]),patch.dict(os.environ,{'WEWORK_WEBHOOK_URL':'https://example.org/webhook'},clear=True),patch('tt_l.research.pipeline.send') as send:
            summary=run(self.settings,{'ai':{}},self.root/'research.db',self.root/'reports',True,
                        http=object(),model=FakeModel(),skip_discovery=True)
            self.assertEqual(len(summary['cards']),1)
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.store.db.execute('SELECT status FROM items').fetchone()[0],'sent')

    def test_source_publication_label_is_parsed_without_feed_date(self):
        html='<div>Publication 06 November 2026</div><main><p>'+('article content '*40)+'</p></main>'
        doc=extract(html.encode(),'text/html','https://example.org/article')
        self.assertEqual(doc['published'],'2026-11-06')

    def test_delivery_acknowledgement_deduplicates_across_days(self):
        self.ready()
        with patch.dict(os.environ,{'WEWORK_WEBHOOK_URL':'https://example.org/webhook'},clear=True),patch('tt_l.research.pipeline.send') as send:
            deliver(self.store,self.settings,True)
            self.ready('2')
            deliver(self.store,self.settings,True)
            self.assertEqual(send.call_count,1)

    def test_ambiguous_delivery_does_not_automatically_resend(self):
        self.ready()
        with patch.dict(os.environ,{'WEWORK_WEBHOOK_URL':'https://example.org/webhook'},clear=True),patch('tt_l.research.pipeline.send',side_effect=TimeoutError) as send:
            deliver(self.store,self.settings,True)
            self.ready('2')
            deliver(self.store,self.settings,True)
            self.assertEqual(send.call_count,1)
        self.assertEqual(self.store.db.execute('SELECT status FROM deliveries').fetchone()[0],'unknown')

    def test_fetch_failure_is_retriable_not_irrelevant(self):
        self.store.add({'url':'https://example.org/broken','source':'test'})
        with patch('tt_l.research.pipeline.document',side_effect=PermissionError('robots_disallowed')):
            r=run(self.settings,{'ai':{}},self.root/'research.db',self.root/'reports',skip_discovery=True)
        self.assertEqual(r['queues'][0]['status'],'pending')
        row=self.store.db.execute('SELECT attempts,retry_at,classification FROM items').fetchone()
        self.assertEqual(row['attempts'],1)
        self.assertIsNone(row['classification'])
        self.assertGreater(row['retry_at'],now())

    def test_metrics_use_all_negative_samples_as_false_positive_denominator(self):
        result=metrics([True,True,False,False],[True,False,True,False])
        self.assertEqual(result['recall'],0.5)
        self.assertEqual(result['false_positive_rate'],0.5)

    def test_pdf_text_and_scan_are_distinguished(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('pypdf optional dependency unavailable')
        writer=PdfWriter()
        writer.add_blank_page(width=200,height=200)
        output=BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError,'ocr_required'):
            extract(output.getvalue(),'application/pdf','https://example.org/a.pdf')


if __name__ == '__main__':
    unittest.main()
