"""Exercise real Firefox filter navigation against intercepted local HTML only."""
import json
from pathlib import Path
import sys
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.session import browser_api


def run():
    results=[]
    with browser_api()() as api:
        browser=api.firefox.launch(headless=True)
        try:
            for scenario in ('provided_filter','padded_label','hidden_filter','changed_pager','unknown_type_privacy','script_changed_category',
                             'provided_category_filter','unknown_category_privacy','script_changed_type','provided_known_category_three_pages','ticket_label_privacy'):
                context=browser.new_context(service_workers='block');calls=[]
                code='PRIVATE_CODE' if scenario in {'unknown_type_privacy','unknown_category_privacy'} else 'FREE'
                if scenario=='provided_known_category_three_pages':code='FREE_EPISODES'
                category_filter=scenario in {'provided_category_filter','unknown_category_privacy','script_changed_type','provided_known_category_three_pages'}
                first=(f'category={code}&type=ALL' if category_filter else f'category=SUBSCRIBED&type={code}')
                start='https://tapas.io/reading-list?category=SUBSCRIBED'
                def respond(route):
                    request=route.request;value=urlsplit(request.url)
                    if request.method!='GET' or value.hostname!='tapas.io' or value.path!='/reading-list':
                        route.abort()
                        raise AssertionError('Unexpected offline-browser request')
                    fields=parse_qs(value.query);filtered=fields.get('category' if category_filter else 'type')==[code]
                    page=int(fields.get('pageNumber',['1'])[0]);calls.append((filtered,page))
                    action=(f''' onclick="location.href='/reading-list?category=RECENT&amp;type={code}';return false"'''
                            if scenario=='script_changed_category' else '')
                    if scenario=='script_changed_type':
                        action=f''' onclick="location.href='/reading-list?category={code}&amp;type=COMICS';return false"'''
                    label=' \n Free \t Episodes \n ' if scenario=='padded_label' else 'Free Episodes'
                    visibility=' style="display:none"' if scenario=='hidden_filter' else ''
                    selected=' on' if filtered else ''
                    following='ALL' if scenario=='changed_pager' else code
                    next_query=(f'category={code}&type=ALL' if category_filter else f'category=SUBSCRIBED&type={following}')
                    last=3 if scenario=='provided_known_category_three_pages' else 2
                    disabled=' disabled' if page==last else ''
                    semantics=('''<aside data-free-tickets="12345" data-ticket-token="PRIVATE_TOKEN" data-gift-expires="PRIVATE_DATE">
                        <p>3 free tickets</p><button onclick="window.claimed=1">Claim</button>
                        <p>PRIVATE_ACCOUNT_NAME</p><button hidden>Redeem</button></aside>'''
                        if scenario=='ticket_label_privacy' else '')
                    html=f'''<!doctype html><html><body>
                    {semantics}
                    <nav class="section-filter"{visibility}><ul class="filter-wrap">
                    <li class="filter-item{selected}" data-type="{code}">
                    <a class="item-title" href="/reading-list?{first}"{action}>{label}</a></li></ul></nav>
                    <ul class="content-list-wrap"><li class="content-item" data-tiara-event-meta-series-id="{41+page}">
                    <a href="/series/example-{page}">Example {page}</a></li></ul>
                    <section class="paging-wrap"><a class="paging__button--next{disabled}"
                    href="/reading-list?pageNumber={page+1}&{next_query}">Next</a></section>
                    </body></html>'''
                    route.fulfill(status=200,content_type='text/html',body=html)
                context.route('**/*',respond)
                page=context.new_page();page.goto(start,wait_until='domcontentloaded')
                adapter=object.__new__(TapasAdapter);adapter.page=page
                adapter.remaining_timeout=lambda:10000;adapter.wait_normal=lambda _:None
                automatic=Mock();adapter.automatic_adapter=lambda:automatic
                before={'authenticated':True,'membership_verified':True,'scope':'subscribed'}
                checkpoints=[]
                if scenario=='provided_known_category_three_pages':
                    adapter._inspect_library_details=Mock(return_value=before)
                    result=adapter.inspect_library(category='free_episodes',on_page=checkpoints.append)
                    assert len(checkpoints)==3 and all(c['scope']=='free_episodes' and not c['account_membership_verified'] for c in checkpoints)
                    adapter._inspect_library_details.assert_called_once_with('subscribed',research=False)
                else:result=adapter._inspect_free_episode_filter(before,max_pages=2)
                assert result['account_inventory_complete'] is False
                assert 'PRIVATE' not in json.dumps(result)
                if scenario=='hidden_filter':
                    assert not result['filter_clicked'] and len(calls)==1
                    automatic.check_session.assert_not_called()
                else:
                    assert result['filter_clicked'] and result['restore_succeeded'] and page.url==start
                    if scenario=='changed_pager':
                        assert not result['complete'] and len(result['series'])==1 and len(calls)==3
                    elif scenario in {'script_changed_category','script_changed_type'}:
                        assert not result['membership_verified'] and not result['complete'] and len(calls)==3
                    else:
                        expected_pages=3 if scenario=='provided_known_category_three_pages' else 2
                        assert result['complete'] and result['membership_verified'] and len(result['series'])==expected_pages
                        assert len(calls)==expected_pages+2 and automatic.check_session.call_count==expected_pages
                    if scenario in {'unknown_type_privacy','unknown_category_privacy'}:assert result['public_type_code'] is None
                    if scenario=='ticket_label_privacy':
                        semantics=result['view_semantics']
                        assert semantics['label_node_counts']['ticket_count_label']==1 and semantics['label_node_counts']['claim']==1
                        assert 'redeem' not in semantics['label_node_counts']
                        assert semantics['attribute_value_types']=={'data-free-tickets':'integer','data-gift-expires':'string'}
                        assert not semantics['controls_activated'] and not semantics['authorization_evidence']
                        assert '12345' not in json.dumps(result) and not page.evaluate('Boolean(window.claimed)')
                results.append({'scenario':scenario,'intercepted_requests':len(calls),'passed':True})
                context.close()
        finally:browser.close()
    return {'browser':'firefox','offline':True,'source_requests':0,'scenarios':results}


if __name__=='__main__':print(json.dumps(run(),indent=2))
