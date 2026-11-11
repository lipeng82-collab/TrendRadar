"""Supported search APIs; discovered links are never treated as evidence."""
import json
import os
from urllib.parse import urlencode

import requests

from tt_l.topic_cards import canonical_url
from .store import digest, now


def search(http, query, store=None, settings=None, item_id=''):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('empty_search_query')
    backend = 'brave' if os.getenv('BRAVE_SEARCH_API_KEY') else 'gdelt'
    cache_key = 'search:'+now()[:10]+':'+backend+':'+digest(query)
    if store:
        cached = store.cached(cache_key)
        if cached is not None:
            return cached
        fee = settings['search_request_usd_ceiling'] if backend == 'brave' else 0
        store.reserve(0, fee, settings['budget'])
        store.event(item_id,'research_search','reserved',backend,reserved_usd=fee)
    try:
        if backend == 'brave':
            # API credentials only go to this fixed official endpoint, never a result URL.
            response = requests.get('https://api.search.brave.com/res/v1/web/search',
                params={'q':query[:400],'count':10},
                headers={'X-Subscription-Token':os.environ['BRAVE_SEARCH_API_KEY'],'Accept':'application/json'},
                timeout=20,allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError('search_api_http_'+str(response.status_code))
            if len(response.content) > 2000000:
                raise ValueError('search_response_too_large')
            results = response.json().get('web',{}).get('results',[])
        else:
            # Public DOC API: real destination article URLs, no search-page scraping.
            url='https://api.gdeltproject.org/api/v2/doc/doc?'+urlencode({
                'query':query[:400],'mode':'artlist','format':'json',
                'maxrecords':10,'timespan':'30d','sort':'datedesc'})
            old_interval=http.interval
            http.interval=max(5.0,old_interval)
            try:
                raw,_,_=http.get(url)
            finally:
                http.interval=old_interval
            results=json.loads(raw).get('articles',[])
        rows=[]
        for r in results:
            url=canonical_url(r.get('url',''))
            if url:
                rows.append({'url':url,'title':r.get('title',''),
                             'published':r.get('seendate',''),
                             'source':backend+' discovery; not evidence'})
        if not rows:
            raise ValueError('search_no_results')
        if store:
            store.cache(cache_key,rows)
        return rows
    except Exception as exc:
        # API error messages can echo auth headers; keep only the class.
        raise RuntimeError('search_'+backend+'_failed:'+type(exc).__name__) from None
