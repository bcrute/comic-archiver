"""Normalize the creator/description markup observed on Tapas series pages."""
import re
from html.parser import HTMLParser
from .reader import ReaderHTML, safe_primitives
from ..errors import MetadataError


def plain_text(parts):
    text=''.join(parts)
    lines=[re.sub(r'[\t\r\f\v ]+',' ',line).strip() for line in text.split('\n')]
    return re.sub(r'\n{3,}','\n\n','\n'.join(lines)).strip()


class SeriesDetailsHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.blocks=[]

    def handle_starttag(self,tag,pairs):
        attrs=dict(pairs)
        classes=set(attrs.get('class','').split())
        scopes=set().union(*(item[1] for item in self.stack)) if self.stack else set()
        kind=None
        if tag=='span' and 'description__body' in classes and 'js-series-description' in scopes:
            kind='description'
        if tag=='p' and 'colophon' in classes and 'detail-row__body--desc' in scopes:
            kind='credits'
        if (tag=='a' and 'name' in classes and 'creator-info__top' in scopes
                and {'detail-row__body--creator','creator-section'} & scopes):
            kind='creator'
        if kind:
            self.blocks.append({'kind':kind,'parts':[],'depth':len(self.stack)})
        if tag=='br':
            for block in self.blocks:
                if block.get('active',True): block['parts'].append('\n')
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append((tag,classes))

    def handle_data(self,text):
        if any(tag in {'script','style'} for tag,classes in self.stack):
            return
        for block in self.blocks:
            if block.get('active',True): block['parts'].append(text)

    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                for block in self.blocks:
                    if block.get('active',True) and block['depth']>=index:
                        block['active']=False
                del self.stack[index:]
                break


def series_details(html,expected_id):
    if not isinstance(html,str) or len(html)>8*1024*1024:
        raise MetadataError('Series metadata HTML exceeds the supported size.')
    identity=ReaderHTML('unused')
    identity.feed(html)
    values=safe_primitives('\n'.join(identity.inline))
    found=next((values[k] for k in ('seriesId','seriesID','series_id') if k in values),None)
    if found is None and len(identity.series_ids)==1:
        found=next(iter(identity.series_ids))
    if type(expected_id) is not int or expected_id<1 or found!=expected_id:
        raise MetadataError('Series detail page does not match the verified series identity.')
    parsed=SeriesDetailsHTML()
    parsed.feed(html)
    by_kind={kind:[] for kind in ('description','credits','creator')}
    for block in parsed.blocks:
        value=plain_text(block['parts'])
        if value and value not in by_kind[block['kind']]:
            by_kind[block['kind']].append(value)
    if any(len(by_kind[k])>1 for k in ('description','credits')):
        raise MetadataError('Desktop/mobile series details disagree; metadata enrichment requires inspection.')
    creators=by_kind['creator']
    if len(creators)>20 or any(len(name)>200 for name in creators):
        raise MetadataError('Creator metadata exceeds the supported size.')
    description=next(iter(by_kind['description']),None)
    credits=next(iter(by_kind['credits']),None)
    if any(value and len(value)>20000 for value in (description,credits)):
        raise MetadataError('Series description or credits exceed the supported size.')
    return {'creators':tuple(creators),'description':description,'credits':credits}
