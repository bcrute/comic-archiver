"""Validate reading-list pager links actually supplied by Tapas."""
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl,urlsplit



LIBRARY_FILTERS = {'free_episodes': ('FREE_EPISODES', 'free episodes'),
                   'wait_until_free': ('WAIT_UNTIL_FREE', 'wait until free')}


def verified_filter_location(url, category):
    """Only observed named Library filters, with their All-content scope."""
    if category not in LIBRARY_FILTERS:
        return None
    location = supplied_filter_location(url)
    if location is None or location['category'] != category or location['type'].upper() != 'ALL':
        return None
    return location


def supplied_filter_location(url):
    """Validate a provided reading-list link; its type code grants no access.

    Unknown category/type codes stay private until a visible named UI control verifies
    their meaning. Never construct a URL from this descriptor.
    """
    if not isinstance(url,str):return None
    try:
        value=urlsplit(url)
        if (value.scheme!='https' or value.hostname!='tapas.io' or value.username or value.password or value.port
                or value.path!='/reading-list' or value.fragment):return None
        pairs=parse_qsl(value.query,keep_blank_values=True);fields=dict(pairs)
        if len(fields)!=len(pairs) or set(fields)-{'pageNumber','type','category'}:return None
        category=fields.get('category','UPDATED');page=fields.get('pageNumber','1')
        code=fields.get('type','ALL') or 'ALL'
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',category) or not re.fullmatch(r'[1-9][0-9]{0,3}',page)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',code)):return None
        return {'page':int(page),'category':category.lower(),'type':code}
    except ValueError:return None


def reading_list_location(url):
    if not isinstance(url,str):return None
    try:
        value=urlsplit(url)
        if (value.scheme!='https' or value.hostname!='tapas.io' or value.username or value.password or value.port
                or value.path!='/reading-list' or value.fragment):return None
        pairs=parse_qsl(value.query,keep_blank_values=True);fields=dict(pairs)
        if len(fields)!=len(pairs) or set(fields)-{'pageNumber','type','category'}:return None
        category=fields.get('category','UPDATED')
        if category not in {'SUBSCRIBED','RECENT','UPDATED'} or fields.get('type','ALL') not in {'','ALL'}:return None
        page=fields.get('pageNumber','1')
        if not re.fullmatch(r'[1-9][0-9]{0,3}',page):return None
        return {'page':int(page),'category':category.lower(),'type':'all'}
    except ValueError:return None


class FilterNavigationHTML(HTMLParser):
    """Source-supplied named filter links and its explicit Next boundary."""
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.stack = []
        self.filters = []
        self.selected = set()
        self.next_controls = []
        self.anchor = None

    def handle_starttag(self, tag, pairs):
        from urllib.parse import urljoin
        attrs = {key: value or '' for key, value in pairs}
        classes = attrs.get('class', '').split()
        if tag not in {'img','br','input','meta','link','hr','source','wbr','area','base','embed','param','track','col'}:
            self.stack.append((tag, attrs))
        in_filters = any('section-filter' in frame.get('class','').split() for _,frame in self.stack)
        if tag != 'a':
            return
        link = urljoin(self.url, attrs.get('href', ''))
        parsed = urlsplit(link)
        if parsed.scheme == 'https' and parsed.netloc == 'm.tapas.io':
            link = parsed._replace(netloc='tapas.io').geturl()
        if in_filters:
            selected_codes = {frame.get('data-type') for _, frame in self.stack
                              if 'filter-item' in frame.get('class','').split() and 'on' in frame.get('class','').split()}
            self.anchor = {'url': link, 'text': [], 'depth': len(self.stack), 'selected_codes': selected_codes}
        in_pager = any('paging-wrap' in frame.get('class','').split() for _,frame in self.stack)
        if in_pager and 'paging__button--next' in classes:
            if len(self.next_controls) >= 2:
                return
            self.next_controls.append({'url': link, 'disabled': 'disabled' in classes or attrs.get('aria-disabled') == 'true'})

    def handle_data(self, text):
        if self.anchor is not None:
            self.anchor['text'].append(text)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0] == tag:
                if self.anchor is not None and index < self.anchor['depth']:
                    anchor, self.anchor = self.anchor, None
                    label = ' '.join(''.join(anchor['text']).lower().split())
                    for category, (code, expected) in LIBRARY_FILTERS.items():
                        if label == expected and verified_filter_location(anchor['url'], category) is not None:
                            if len(self.filters) <= 20:
                                self.filters.append({'category': category, 'url': anchor['url']})
                            if code in anchor['selected_codes']:
                                self.selected.add(category)
                del self.stack[index:]
                break
