"""Direct public HTTP, robots checks, HTML/PDF and RSS/Atom/RDF extraction.

Never sends cookies, bypasses paywalls, runs JS, or uses a reader proxy.
"""
from __future__ import annotations

from html.parser import HTMLParser
from datetime import datetime
from io import BytesIO
from ipaddress import ip_address
import re
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests

from tt_l.topic_cards import canonical_url
from .store import digest, now

AGENT = 'TTLResearchBot/1.0'


def public_url(url, resolve=True):
    clean = canonical_url(url)
    if not clean:
        raise ValueError('unsafe_url')
    host = urlsplit(clean).hostname
    if resolve:
        addresses = socket.getaddrinfo(host, urlsplit(clean).port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('non_public_address')
    return clean


class PublicHTTP:
    def __init__(self, timeout=20, max_bytes=10000000, interval=1.0):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'User-Agent': AGENT})
        self.timeout, self.max_bytes, self.interval = timeout, max_bytes, interval
        self.last = {}
        self.robots = {}

    def _raw(self, url, enforce_robots=False):
        for _ in range(6):
            url = public_url(url)
            if enforce_robots:
                self._policy(url)
            host = urlsplit(url).netloc
            time.sleep(max(0, self.interval - (time.monotonic()-self.last.get(host, 0))))
            self.last[host] = time.monotonic()
            self.session.cookies.clear()
            with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers['Location'])
                    continue
                if r.status_code == 404:
                    raise FileNotFoundError('http_404')
                r.raise_for_status()
                data = bytearray()
                for chunk in r.iter_content(32768):
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise ValueError('response_too_large')
                return bytes(data), r.headers.get('Content-Type', ''), url
        raise ValueError('redirect_limit')

    def _policy(self, url):
        url = public_url(url)
        origin = '{0.scheme}://{0.netloc}'.format(urlsplit(url))
        if origin not in self.robots:
            rp = RobotFileParser(origin + '/robots.txt')
            try:
                raw, _, _ = self._raw(origin+'/robots.txt')
                rp.parse(raw.decode('utf-8', errors='replace').splitlines())
            except FileNotFoundError:
                rp.parse([])  # 404: no robots policy published.
            except Exception as exc:
                raise RuntimeError('robots_unavailable:'+type(exc).__name__) from None
            self.robots[origin] = rp
        if not self.robots[origin].can_fetch(AGENT, url):
            raise PermissionError('robots_disallowed')
        delay = self.robots[origin].crawl_delay(AGENT) or 0
        time.sleep(max(0, delay-(time.monotonic()-self.last.get(urlsplit(url).netloc, 0))))

    def get(self, url):
        return self._raw(public_url(url), enforce_robots=True)


class HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.blocks, self.links, self.meta = [], [], [], {}
        self.buf, self.link, self.title = [], None, ''
        self.title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            self.meta[attrs.get('property', attrs.get('name', ''))] = attrs.get('content', '')
        if tag in {'meta', 'img', 'br', 'hr', 'input', 'link', 'source', 'wbr'}:
            return
        if tag in {'p', 'h1', 'h2', 'h3', 'li', 'blockquote', 'article', 'main'}:
            self.flush()
        self.stack.append(tag)
        if tag == 'a':
            self.link = [attrs.get('href', ''), []]
        if tag == 'time' and attrs.get('datetime'):
            self.meta.setdefault('article:published_time', attrs['datetime'])

    def handle_endtag(self, tag):
        if tag == 'a' and self.link:
            self.links.append((self.link[0], ''.join(self.link[1]).strip()))
            self.link = None
        if tag in {'p', 'h1', 'h2', 'h3', 'li', 'blockquote', 'article', 'main', 'section', 'div'}:
            self.flush()
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack)-1-self.stack[::-1].index(tag)]

    def handle_data(self, text):
        if 'title' in self.stack:
            self.title_parts.append(text)
        if self.link:
            self.link[1].append(text)
        if not set(self.stack) & {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'title'}:
            self.buf.append(text)

    def flush(self):
        s = re.sub(r'\s+', ' ', ''.join(self.buf)).strip()
        if len(s) >= 25:
            self.blocks.append((s, bool(set(self.stack) & {'article', 'main'})))
        self.buf = []


def extract(raw, content_type, url, published=''):
    links, title, meta = [], '', {}
    if raw.startswith(b'%PDF'):
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(raw))
        if pdf.is_encrypted:
            raise ValueError('encrypted_pdf')
        if len(pdf.pages) > 250:
            raise ValueError('pdf_page_limit')
        paragraphs = []
        for page, obj in enumerate(pdf.pages, 1):
            for part in (obj.extract_text() or '').split('\n\n'):
                text = re.sub(r'\s+', ' ', part).strip()
                if text:
                    paragraphs.append({'id': f'p{len(paragraphs)+1}', 'text': text, 'page': page})
        if sum(len(p['text']) for p in paragraphs) < 150:
            raise ValueError('pdf_scan_or_empty_ocr_required')
    else:
        if 'html' not in content_type.lower():
            raise ValueError('unsupported_content_type')
        html = raw.decode('utf-8', errors='replace')
        if re.search(r'"isAccessibleForFree"\s*:\s*false|class=["\'][^"\']*paywall', html, re.I):
            raise ValueError('paywall_no_bypass')
        parser = HTML()
        parser.feed(html)
        parser.flush()
        body = [s for s, main in parser.blocks if main] or [s for s, _ in parser.blocks]
        # Short/JS/challenge/abstract-only documents cannot be silently called full text.
        if sum(map(len, body)) < 350 or re.search('checking your browser|enable javascript and cookies', ' '.join(body), re.I):
            raise ValueError('incomplete_body')
        if '/articles/' in url and 'nature.com' in url and 'data-section="body"' not in html and 'id="Sec' not in html:
            raise ValueError('nature_fulltext_unconfirmed')
        paragraphs = [{'id': f'p{i}', 'text': s} for i, s in enumerate(dict.fromkeys(body), 1)]
        links = [{'url': canonical_url(urljoin(url, u)), 'label': label}
                 for u, label in parser.links if canonical_url(urljoin(url, u))]
        meta = parser.meta
        title = meta.get('og:title') or ''.join(parser.title_parts).strip()
        published = meta.get('article:published_time') or meta.get('citation_publication_date') or published
        if not published:
            match = re.search(r'Publication\s+(\d{1,2} [A-Za-z]+ \d{4})', re.sub('<[^>]+>', ' ', html))
            if match:
                try:
                    published = datetime.strptime(match[1], '%d %B %Y').date().isoformat()
                except ValueError:
                    pass
    return {'url': url, 'published': published, 'date_status': 'source_supplied' if published else 'unknown',
            'retrieved_at': now(), 'sha256': digest('\n'.join(p['text'] for p in paragraphs)),
            'status': 'extracted', 'coverage': 'public_body_extracted_not_publisher_certified',
            'title': title, 'paragraphs': paragraphs, 'links': links}


def document(http, url, published=''):
    landing = url
    # The EC detail page is a JS shell; its official print endpoint is public PDF.
    match = re.search(r'ec\.europa\.eu/commission/presscorner/detail/en/(ip_\d+_\d+)', url, re.I)
    if match:
        code = match[1]
        url = f'https://ec.europa.eu/commission/presscorner/api/files/document/print/en/{code.lower()}/{code.upper()}_EN.pdf'
    raw, kind, final = http.get(url)
    if 'html' in kind and any(d in (urlsplit(final).hostname or '') for d in ('bis.org','ecb.europa.eu','oecd.org')):
        parser = HTML()
        parser.feed(raw.decode('utf-8', errors='replace'))
        pdf = parser.meta.get('citation_pdf_url')
        candidates = [(urljoin(final,u), label) for u,label in parser.links
                      if re.search(r'\.pdf(?:\?|$)',u,re.I) and
                      re.search(r'full|download|pdf|report|paper',label,re.I)]
        if not pdf and candidates:
            pdf = candidates[0][0]
        if pdf and urlsplit(pdf).hostname == urlsplit(final).hostname:
            pdf_raw, pdf_kind, pdf_final = http.get(pdf)
            doc = extract(pdf_raw,pdf_kind,pdf_final,published)
            doc['landing_url'] = landing
            return doc
    result = extract(raw, kind, final, published)
    if landing != final:
        result['landing_url'] = landing
    return result


def feed_rows(raw, source):
    root = ET.fromstring(raw)
    rows = []
    def local(tag):
        return tag.rsplit('}', 1)[-1]
    for item in root.iter():
        if local(item.tag) not in {'item', 'entry'}:
            continue
        fields = {}
        for child in item:
            key = local(child.tag)
            if key == 'link':
                if child.attrib.get('rel', 'alternate') == 'alternate':
                    fields['url'] = child.attrib.get('href') or child.text or ''
            else:
                fields[key] = ''.join(child.itertext()).strip()
        url = canonical_url(fields.get('url', '') or fields.get('id', ''))
        if url:
            rows.append({'url': url, 'title': fields.get('title', ''), 'source': source,
                         'published': fields.get('published') or fields.get('pubDate') or fields.get('date', '')})
    return rows


def index_rows(raw, base, pattern, source):
    parser = HTML()
    parser.feed(raw.decode('utf-8', errors='replace'))
    return [{'url': u, 'title': label, 'source': source, 'published': ''}
            for href, label in parser.links
            if (u := canonical_url(urljoin(base, href))) and re.search(pattern, u)]
