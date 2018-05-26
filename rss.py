import re
import time
import hashlib
from xml.sax.saxutils import unescape as sax_unescape
from flask import request
from lxml import etree
import requests
import readability

try:
    unichr(0)
except NameError:
    unichr = chr
try:
    xrange(0)
except NameError:
    xrange = range

def unescape(s):
    deref_ncr = lambda m: unichr(int(m.group(1), 16)) # '&#x2F;' -> '/'
    s = re.sub('&#[Xx]([A-Fa-f0-9]+);', deref_ncr, s)
    entities = {'&quot;': '"', '&apos;': "'"}
    return sax_unescape(s, entities)

def fetch_article(url):
    # load page and process with readability
    try:
        start = time.time()
        headers = { 'User-Agent': 'Mozilla/5.0' }
        response = requests.get(url, timeout=15, headers=headers)
        article = ''
        if response.status_code != 200:
            article += ('<div>HTTP error fetching article: %d</div>\n' %
                        response.status_code)
        content_type = response.headers.get("content-type", "unknown/unknown")
        if content_type.startswith("text/"):
            doc = readability.Document(response.text)
            title = doc.short_title()
            body = doc.summary(html_partial=True)
            article += '<hr><div>%s</div><hr>\n' % body
        else:
            article += ('<div>Non-text content-type: %s</div>\n' %
                        content_type)
        elapsed = time.time() - start
        now = time.strftime('%c %Z')
        article += (
            '<p><small><em>'
            'Fetched in %(elapsed).3fs at %(now)s<br>\n'
            'Original title: %(title)s\n'
            '</em></small></p>'
            ) % locals()
        return article
    except requests.exceptions.RequestException as e:
        return 'Failed to fetch article: %s' % e

def insert_donation_request(guid):
    h = hashlib.sha1(guid).hexdigest()
    if h.startswith('0'):
        return '''
<hr><p>hnrss is a labor of love, but if the project has made your job
or hobby project easier and you want to show some gratitude, <a
href="https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&amp;hosted_button_id=ZP9Q7QUNS3QYY">donations are very much
appreciated</a>. Thanks!</p>
        '''
    else:
        return ''

class RSS(object):
    def __init__(self, api_response, title, link='https://news.ycombinator.com/'):
        self.api_response = api_response

        nsmap = {
            'dc': 'http://purl.org/dc/elements/1.1/',
            'atom': 'http://www.w3.org/2005/Atom',
        }
        self.rss_root = etree.Element('rss', version='2.0', nsmap=nsmap)
        self.rss_channel = etree.SubElement(self.rss_root, 'channel')

        self.add_element(self.rss_channel, 'title', title)
        self.add_element(self.rss_channel, 'link', link)
        self.add_element(self.rss_channel, 'description', 'Hacker News RSS')
        self.add_element(self.rss_channel, 'docs', 'https://edavis.github.io/hnrss/')
        self.add_element(self.rss_channel, 'generator', 'https://github.com/edavis/hnrss')
        self.add_element(self.rss_channel, 'lastBuildDate', self.generate_rfc2822())

        # FIXME: Is there a way to tell Flask or nginx we're running under HTTPS so this is correct off the bat?
        atom_link = request.url.replace('http://', 'https://')
        atom_link = atom_link.replace('"', '%22').replace(' ', '%20')
        self.add_element(self.rss_channel, '{http://www.w3.org/2005/Atom}link', text='', rel='self', type='application/rss+xml', href=atom_link)

        if 'hits' in api_response:
            self.generate_body()

    def generate_body(self):
        for hit in self.api_response['hits']:
            rss_item = etree.SubElement(self.rss_channel, 'item')
            hn_url = 'https://news.ycombinator.com/item?id=%s' % hit['objectID']
            url = hit.get('url') or hn_url
            tags = hit.get('_tags', [])

            title = hit.get('title')
            author = hit.get('author')
            comments = hit.get('num_comments') or 0
            points = hit.get('points') or 0
            story_text = hit.get('story_text')
            if story_text:
                article = '<hr>%s</hr>' % story_text
            else:
                article = fetch_article(url)

            pointstr = "%d point" % points
            if points != 1:
                pointstr += "s"
            commentstr = "%d comment" % comments
            if comments != 1:
                commentstr += "s"

            body = (
                '<div>(<b>HN:</b> '
                '%(pointstr)s, '
                '<a href="%(hn_url)s">%(commentstr)s</a>'
                ')</div> '
                '%(article)s'
                ) % locals()


            def rss_add(*args, **kwargs):
                self.add_element(rss_item, *args, **kwargs)
            rss_add('title', title)
            rss_add('description', body)
            rss_add('link', url)
            rss_add('{http://purl.org/dc/elements/1.1/}creator', author)
            rss_add('pubDate', self.generate_rfc2822(hit.get('created_at_i')))
            rss_add('comments', hn_url)
            rss_add('guid', hn_url, isPermaLink='false')

    def response(self):
        rss_xml = etree.tostring(
            self.rss_root, pretty_print=True, encoding='UTF-8', xml_declaration=True,
        )

        if self.api_response.get('hits'):
            latest = max(hit['created_at_i'] for hit in self.api_response['hits'])
            last_modified = self.generate_rfc2822(latest)

            # Set max-age=N to the average number of seconds between new items
            timestamps = sorted(map(lambda h: h['created_at_i'], self.api_response['hits']), reverse=True)
            seconds = sum(timestamps[idx] - timestamps[idx+1] for idx in xrange(0, len(timestamps) - 1)) / float(len(timestamps))
        else:
            last_modified = self.generate_rfc2822()
            seconds = 5 * 60

        # Cap between 5 minutes and 1 hour
        if seconds < (5 * 60):
            seconds = (5 * 60)
        elif seconds > (60 * 60):
            seconds = (60 * 60)

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'Last-Modified': last_modified.replace('+0000', 'GMT'),
            'Cache-Control': 'max-age=%d' % int(seconds),
            'Expires': self.generate_rfc2822(int(time.time() + seconds)).replace('+0000', 'GMT'),
        }

        return (rss_xml, 200, headers)

    def add_element(self, parent, tag, text, **attrs):
        el = etree.Element(tag, attrs)
        el.text = text
        parent.append(el)
        return el

    def generate_rfc2822(self, secs=None):
        t = time.gmtime(secs)
        return time.strftime('%a, %d %b %Y %H:%M:%S +0000', t)
