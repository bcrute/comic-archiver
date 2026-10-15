"""Validate reading-list pager links actually supplied by Tapas."""
import re
from urllib.parse import parse_qsl,urlsplit

LIBRARY_NAVIGATION_VERSION = 3


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
