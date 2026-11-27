"""An episode's comments as Tapas shows them under it, newest first: who wrote each, when, its words and counts."""
import re
from html.parser import HTMLParser

from ..extras import COMMENT_CHARACTERS, NAME_CHARACTERS, count, text
from ..models import ArchiveError

PAGE_SIZE = 10


class CommentRows(HTMLParser):
    """The top-level comments of one page of Tapas's comment list. Replies are counted, not read."""
    FIELDS = {'writer__name': 'name', 'writer__date': 'date', 'js-comment-body': 'text', 'js-like-cnt': 'likes'}
    EMPTY = {'br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'i'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.field, self.depth, self.parts = [], None, 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get('class') or '').split()
        if self.field is not None:
            if tag == 'br':
                self.parts.append('\n')
            elif tag not in self.EMPTY:
                self.depth += 1
            return
        found = re.fullmatch(r'comment-row-([0-9]{1,20})', attrs.get('id') or '')
        if tag == 'div' and 'js-comment-parent-row' in classes and found:
            self.rows.append({'id': found[1]})
            return
        if not self.rows:
            return
        if 'js-toggle-reply-btn' in classes:
            replies = attrs.get('data-reply-cnt') or ''
            self.rows[-1]['replies'] = int(replies) if replies.isdigit() and len(replies) < 10 else None
            return
        field = next((name for key, name in self.FIELDS.items() if key in classes), None)
        if field is not None and field not in self.rows[-1] and tag not in self.EMPTY:
            self.field, self.depth, self.parts = field, 1, []

    def handle_endtag(self, tag):
        if self.field is None or tag in self.EMPTY:
            return
        self.depth -= 1
        if not self.depth:
            words = ''.join(self.parts)
            row = self.rows[-1]
            if self.field == 'likes':
                words = words.strip()
                row['likes'] = int(words) if words.isdigit() and len(words) < 10 else None
            else:
                row[self.field] = text(words, COMMENT_CHARACTERS if self.field == 'text' else NAME_CHARACTERS)
            self.field = None

    def handle_data(self, data):
        if self.field is not None:
            self.parts.append(data)


def comment_page(html):
    """The comments of one page, each with the fields Tapas showed; a page without any is an empty list."""
    if not isinstance(html, str) or len(html) > 4 * 1024 * 1024:
        raise ArchiveError('Tapas returned an unexpected comment page.')
    parser = CommentRows()
    parser.feed(html)
    parser.close()
    return [{'id': row['id'], 'name': row.get('name'), 'date': row.get('date'), 'text': row.get('text'),
             'likes': count(row.get('likes')), 'replies': count(row.get('replies'))} for row in parser.rows]


def episode_comments(json_get, key, limit, check=None):
    """Up to limit comments of an episode, newest first, through json_get (Tapas's own paced requests)."""
    if not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', key):
        raise ArchiveError('Use an observed numeric episode identity.')
    comments, seen, page, since, total, complete = [], set(), 1, None, None, False
    for _ in range(-(-limit // PAGE_SIZE) + 1):
        if callable(check):
            check()
        query = f'page={page}&sort=NEWEST' + (f'&since={since}' if since is not None else '')
        data = json_get(f'/comment/{key}?{query}')
        pagination = data.get('pagination')
        if total is None:
            total = count(data.get('total_comment_cnt'))
        for row in comment_page(data.get('html')):
            if row['id'] not in seen and len(comments) < limit:
                seen.add(row['id'])
                comments.append(row)
        if not isinstance(pagination, dict) or pagination.get('has_next') is not True:
            complete = isinstance(pagination, dict) and pagination.get('has_next') is False
            break
        if len(comments) >= limit:
            break
        since = pagination.get('since') if type(pagination.get('since')) is int else since
        # Pages are asked for one after another. The answer's own page number is not used: whether it names
        # the page just read or the one that follows, adding to it could pass over a page.
        page += 1
    # The total counts replies too, so the list is complete when Tapas says there is no next page.
    return {'comments': comments, 'total': total, 'complete': complete}
