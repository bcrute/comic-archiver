#!/usr/bin/env python3
"""Offline Firefox smoke test of the desktop app's dashboard, using a synthetic local library.

All browser requests are intercepted locally; no Tapas traffic, account login,
public server, session import or deployment is involved. Requires Playwright.
"""
from dataclasses import replace
import argparse
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
import shutil
import uuid
from pathlib import Path
import struct
import tempfile
import threading
import traceback
from urllib.parse import urlsplit
import zlib
from unittest.mock import patch

from comic_archive import sites
from comic_archive.archive import Store
from comic_archive.inventory import Inventory
from comic_archive.locking import LibraryLock
from comic_archive.local_web import LOCAL_OWNER, LocalApplication, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.worker import private_json


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(b'\0\x40\xa0\x80'))+chunk(b'IEND',b''))


def navigation_controls(page, site):
    """The dashboard and library pages share one header; only their active page differs."""
    from playwright.sync_api import expect
    snapshot = """node => {const copy=node.cloneNode(true); copy.querySelectorAll('[aria-current]').forEach(link => link.removeAttribute('aria-current')); return copy.outerHTML;}"""
    expect(page.locator('#save-nav')).to_have_text('Dashboard')
    expect(page.locator('.brand')).to_have_text('Comic Archiver')
    expect(page.locator('#save-nav')).to_have_attribute('href','/')
    expect(page.locator('#save-nav')).to_have_attribute('aria-current','page')
    expect(page.locator('header nav a')).to_have_text(['Dashboard','Library','Settings'])
    header = page.locator('header').evaluate(snapshot)
    other = page.context.new_page()
    try:
        other.goto(site+'/library')
        expect(other.locator('#notice')).to_have_text('Saved files loaded.')
        expect(other.locator('#library-nav')).to_have_attribute('aria-current','page')
        expect(other.locator('nav [aria-current=page]')).to_have_count(1)
        assert other.locator('header').evaluate(snapshot) == header
        expect(other.locator('#theme-toggle')).to_have_count(1)
        other.locator('#save-nav').click()
        expect(other.locator('#save-nav')).to_have_attribute('aria-current','page')
        expect(other.locator('#account-download')).to_be_visible()
    finally:
        other.close()


def held_card_messages(page):
    """Each reason a download stopped is said plainly, with what to do; Go waits out a wait the site asked for."""
    card = lambda account, status=None: page.evaluate("""([account, status]) => {
        const saved = [state.account, state.status, state.accountReceivedAt, state.lastAccountRefreshAt];
        state.account = {...state.account, ...account}; state.status = {...state.status, ...(status || {})};
        state.accountReceivedAt = state.lastAccountRefreshAt = Date.now(); renderAccount();
        const read = id => $(id).textContent, result = {status:read('account-status'), title:read('account-save-title'),
            pill:read('dashboard-state'), detail:read('account-detail'), activity:read('account-activity'),
            worker:read('dashboard-worker'), go:$('account-go').disabled, settings:!$('account-settings-link').hidden};
        [state.account, state.status, state.accountReceivedAt, state.lastAccountRefreshAt] = saved; renderAccount(); return result;}""", [account, status])
    held = lambda code, **extra: card({'status':'held', 'code':code, **extra})
    value = held('source_refused', go_after_seconds=600)
    assert 'Tapas asked the app to slow down' in value['status'] and 'Go works again in about 10 minutes' in value['status'], value
    assert value['go'] and value['settings'], value
    for code in ('source_refused', 'source_recovery_limit'):
        value = held(code)
        assert 'choose a slower speed in Settings' in value['status'] and not value['go'] and value['settings'], value
    for code, text in (('no_progress', 'the last few tries saved nothing new'), ('scan_no_progress', 'saved nothing new'),
                       ('network_recovery_limit', 'Tapas could not be reached several times'),
                       ('source_access_changed', 'still would not send an episode'),
                       ('cancelled', 'Paused. Hit Go to continue'),
                       ('window_changed', 'the download stopped unexpectedly'), ('controller_failed', 'stopped unexpectedly')):
        value = held(code)
        assert text in value['status'] and not value['go'] and not value['settings'], (code, value)
        assert value['pill'] == 'Paused', value
    value = held('archive_incomplete', audit={'missing_recorded_accessible_episode_count':3})
    assert value['status'].startswith('Finished, but 3 episodes could not be saved.'), value
    assert (value['title'], value['pill']) == ('Finished, with episodes missing', 'Finished'), value
    assert 'rough guess' not in value['status']
    value = held('archive_incomplete', audit={'missing_recorded_accessible_episode_count':1})
    assert value['status'].startswith('Finished, but 1 episode could not be saved.'), value
    assert held('archive_incomplete')['status'].startswith('Finished, but some saved files need another check.')
    value = card({'status':'stopped', 'code':None, 'go_after_seconds':40})
    assert 'Go works again in a few seconds' in value['status'] and value['go'], value
    assert card({'status':'stopped', 'code':None, 'go_after_seconds':0})['go'] is False
    value = card({'status':'finished', 'code':None}, {'episodes_by_status':{'archived':0}})
    assert 'No episodes your Tapas account can save were found' in value['status'], value
    value = card({'status':'finished', 'code':None}, {'episodes_by_status':{'archived':12}})
    assert value['status'].startswith('Finished. Every episode your account can read is saved.'), value
    # A running step, said plainly; the waits the download sets itself are not stalls.
    running = {'status':'running', 'code':None, 'phase':'archive'}
    value = card(running, {'progress':{'status':'running', 'phase':'archive', 'completed':2, 'downloaded_pages':31, 'downloaded_chapters':1}})
    assert value['detail'].endswith('2 saved in this step · 30 images · 1 novel chapter.'), value
    value = card(running, {'progress':{'status':'running', 'phase':'scan', 'series_checks_completed':4, 'series_check_target_count':10}})
    assert value['detail'].endswith('Checked 4 of 10 series in this step.'), value
    value = card(running, {'progress':{'status':'running', 'phase':'archive', 'completed':1, 'downloaded_pages':1, 'downloaded_chapters':0}})
    assert value['detail'].endswith('1 saved in this step · 1 image.'), value
    assert 'cooldown' not in card(running, {'progress':{'status':'running', 'phase':'metadata'}, 'source_recovery':{'automatic':True}})['detail']
    value = card(running | {'retry':{'reason':'images', 'seconds':240}})
    assert value['activity'] == 'Some images did not arrive. The download tries them again by itself in about 4 minutes.', value
    # Before the first sign-in the stopped downloader is not a fault.
    value = card({'status':None, 'code':None}, {'session_configured':False, 'worker':{'reported_state':'stopped'}})
    assert value['worker'] == 'Downloader · Starts after sign-in', value
    value = card({'status':'held', 'code':'login_required'}, {'session_configured':True, 'worker':{'reported_state':'ready'}})
    assert value['status'].startswith('Paused: Tapas signed you out.'), value
    # Just after a sign-in is kept, a downloader that has not reported yet is starting, not offline; later it is offline.
    starting = lambda since: page.evaluate("""([since]) => {
        const saved = [state.siteAuth, state.connectedAt, state.status, state.account];
        state.account = {...state.account, status:'stopped', code:null}; state.siteAuth = {status:'connected'}; state.connectedAt = Date.now() - since;
        state.status = {...state.status, session_configured:true, worker:{reported_state:'stopped'}}; renderAccount();
        const result = [$('account-status').textContent, $('dashboard-worker').textContent, $('site-signin').hidden];
        [state.siteAuth, state.connectedAt, state.status, state.account] = saved; renderAccount(); return result;}""", [since])
    assert starting(5000) == ['Starting the downloader…', 'Downloader · Starting', True], starting(5000)
    late = starting(130000)
    assert late[0].startswith('The downloader is offline.') and late[1] == 'Downloader · Offline' and late[2] is False, late
    # A failure the hidden notice bar would have shown reaches the card and Settings, and clears after a while.
    page.evaluate("notice('PLAIN FAILURE', true)")
    for selector in ('#site-signin-status', '#settings-status'):
        assert page.locator(selector).text_content() == 'PLAIN FAILURE'
    page.evaluate("state.accountMessage.until = Date.now() - 1; poll()")
    page.wait_for_function("document.getElementById('settings-status').textContent === ''")
    expected = ('Saving runs only while this window is open.' if page.evaluate('ownWindow()') else 'You can close this page')
    assert expected in page.locator('#dashboard-footnote').text_content()


def simple_account_controls(page, registry, workspace, screenshot):
    """The dashboard has one account action, with its choices in Settings."""
    from playwright.sync_api import expect
    page.emulate_media(color_scheme='dark')
    expect(page.locator('#theme-toggle')).to_have_attribute('aria-label','Switch to light mode')
    expect(page.locator('#theme-toggle')).to_have_text('')
    expect(page.locator('#theme-toggle .theme-sun')).to_be_visible()
    expect(page.locator('#theme-toggle .theme-moon')).to_be_hidden()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    page.locator('#theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme','light')
    expect(page.locator('#theme-toggle')).to_have_attribute('aria-label','Switch to dark mode')
    expect(page.locator('#theme-toggle .theme-moon')).to_be_visible()
    expect(page.locator('#theme-toggle .theme-sun')).to_be_hidden()
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-theme','light')
    page.locator('#theme-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    page.emulate_media(color_scheme='light')
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    expect(page.locator('#account-download')).to_be_visible()
    # Keep an existing user's saved theme when the browser setting key changes.
    page.evaluate("() => {localStorage.removeItem('comic-archive-theme'); localStorage.setItem('comic-archive-theme', 'dark');}")
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    assert page.evaluate("localStorage.getItem('comic-archive-theme')") == 'dark'
    # Settings is in the menu. Cloud storage is set up there, and the card offers the way to it.
    for selector in ('#settings-nav', '#cloud-entry'):
        expect(page.locator(selector)).to_be_visible()
    expect(page.locator('#cloud-options')).to_be_hidden()
    # The card's only choice is the site, at its top. The speed and mobile data (mobile_data_controls) are on the
    # Downloads tab of Settings, and where episodes go on its Storage tab (b2_setup_controls).
    assert page.locator('#account-download select').evaluate_all('nodes => nodes.map(node => node.id)') == ['site']
    assert page.locator('#account-download input').evaluate_all('nodes => nodes.map(node => node.id)') == []
    for selector in ('#account-speed', '#account-network', '#cloud-choice', '#account-settings-link'):
        expect(page.locator(selector)).to_be_hidden()
    # A tall saved-library card must not bury the only cloud setup entry.
    expect(page.locator('#cloud-entry')).to_be_in_viewport()
    page.locator('#library-locations').evaluate("node => node.style.minHeight = '1400px'")
    with patch.object(registry.cloud_signin, 'start') as connect:
        # Settings is a page of its own, with the dashboard set aside; the card's way to cloud storage opens its
        # B2 tab.
        page.locator('#cloud-entry a').click()
        expect(page.locator('#cloud-options')).to_be_visible()
        expect(page.locator('#settings-nav')).to_have_attribute('aria-current', 'page')
        expect(page.locator('header nav [aria-current=page]')).to_have_count(1)
        expect(page.locator('.settings-tabs a:visible')).to_have_text(['Downloads', 'Storage', 'Backblaze B2'])
        expect(page.locator('.settings-tabs [aria-current=page]')).to_have_text('Backblaze B2')
        expect(page.locator('#cloud-title')).to_be_in_viewport()
        for selector in ('#account-download', '#library-overview', '#dashboard-heading', '#storage-page', '#downloads-settings'):
            expect(page.locator(selector)).to_be_hidden()
        expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(6)
        expect(page.locator('#b2-login')).to_be_visible()
        expect(page.locator('#b2-application-key')).to_have_value('')
        # Each step reads in the order it is done: the page to open, then what to do there.
        steps = page.locator('#cloud-options .cloud-steps > li')
        for index, href in ((0, 'sign-up/s3'), (1, 'b2_buckets.htm'), (2, 'app_keys.htm')):
            step = steps.nth(index)
            assert (step.locator(f'a[href$="{href}"]').bounding_box()['y']
                    < step.locator('p:not(.step-links)').first.bounding_box()['y']), href
        connect.assert_not_called()
        # Each tab shows in place of the others. Storage's B2 card leads back to B2's tab.
        page.locator('#settings-tab-storage').click()
        expect(page.locator('#storage-page')).to_be_visible()
        expect(page.locator('#cloud-options')).to_be_hidden()
        expect(page.locator('.settings-tabs [aria-current=page]')).to_have_text('Storage')
        expect(page.locator('#storage-b2-pill')).to_have_text('Not set up')
        for selector in ('#storage-b2-files', '#storage-b2-open', '#storage-b2-stored'):
            expect(page.locator(selector)).to_be_hidden()
        expect(page.locator('#storage-b2-setup')).to_have_text('Set up')
        page.locator('#storage-b2-setup').click()
        expect(page.locator('#cloud-options')).to_be_visible()
        expect(page.locator('#storage-page')).to_be_hidden()
        page.locator('#settings-tab-downloads').click()
        expect(page.locator('#downloads-settings')).to_be_visible()
        expect(page.locator('#account-speed')).to_be_visible()
        for selector in ('#storage-page', '#cloud-options'):
            expect(page.locator(selector)).to_be_hidden()
        # Back from Settings leads to the dashboard.
        page.locator('#settings-back').click()
        expect(page.locator('#settings-page')).to_be_hidden()
        expect(page.locator('#account-download')).to_be_visible()
        expect(page.locator('#save-nav')).to_have_attribute('aria-current', 'page')
        # The browser's own Back and Forward move between the dashboard and the tabs too.
        page.go_back()
        expect(page.locator('#downloads-settings')).to_be_visible()
        page.go_back()
        expect(page.locator('#cloud-options')).to_be_visible()
        page.go_forward(); page.go_forward()
        expect(page.locator('#account-download')).to_be_visible()
        # The earlier addresses of the Storage and B2 pages open their tabs.
        for old, panel in (('storage', '#storage-page'), ('b2', '#cloud-options')):
            page.evaluate(f"location.hash = '{old}'")
            expect(page.locator(panel)).to_be_visible()
            assert page.evaluate('location.hash') == '#settings/' + old
        page.locator('#settings-back').click()
        expect(page.locator('#account-download')).to_be_visible()
    page.locator('#library-locations').evaluate("node => node.style.minHeight = ''")
    page.evaluate('scrollTo(0,0)')
    expect(page.locator('#account-go')).to_be_enabled()
    expect(page.locator('#account-status')).to_contain_text('Ready. Hit Go')
    expect(page.locator('#account-save-title')).to_have_text('Ready to save')
    expect(page.locator('#dashboard-state')).to_have_text('Ready')
    expect(page.locator('#dashboard-session')).to_have_text('Tapas · Session saved')
    expect(page.locator('#dashboard-worker')).to_have_text('Downloader · Ready')
    finished=registry.account_status(LOCAL_OWNER,workspace.id) | {'status':'finished','phase':'finished'}
    with patch.object(registry,'account_status',return_value=finished):
        page.evaluate('poll()')
        expect(page.locator('#dashboard-state')).to_have_text('Finished')
        expect(page.locator('#account-save-title')).to_have_text('This run is finished')
    worker_status=workspace.session/'worker/status.json'
    worker_original=worker_status.read_bytes()
    private_json(worker_status,{'state':'stopped'})
    page.evaluate('poll()')
    expect(page.locator('#dashboard-state')).to_have_text('Offline')
    expect(page.locator('#site-signin')).to_be_visible()
    # A downloader that is still starting is said to be starting, without offering to sign in again.
    private_json(worker_status,{'state':'starting'})
    page.evaluate('poll()')
    expect(page.locator('#dashboard-state')).to_have_text('Starting')
    expect(page.locator('#dashboard-worker')).to_have_text('Downloader · Starting')
    expect(page.locator('#account-status')).to_contain_text('Starting the downloader')
    expect(page.locator('#site-signin')).to_be_hidden()
    expect(page.locator('#account-go')).to_be_disabled()
    worker_status.write_bytes(worker_original)
    page.evaluate('poll()')
    expect(page.locator('#dashboard-state')).to_have_text('Ready')
    # The speed starts at the quickest and can be chosen only while nothing is running.
    expect(page.locator('#account-speed')).to_have_value('fast')
    expect(page.locator('#account-speed')).to_be_enabled()
    expect(page.locator('#account-speed-help')).to_contain_text('The quickest')
    page.locator('#account-go').click()
    expect(page.locator('#account-status')).to_contain_text('Finding series')
    expect(page.locator('#account-speed')).to_be_disabled()
    expect(page.locator('#account-speed-help')).to_have_text('Pause to change the speed.')
    expect(page.locator('#account-go')).to_be_disabled()
    page.evaluate('poll()')
    assert not list((workspace.session/'worker/requests').glob('*.json'))
    from comic_archive import account_export
    account_export.worker_tick(workspace.session, workspace.library)
    account_export.worker_tick(workspace.session, workspace.library)
    requests = list((workspace.session/'worker/requests').glob('*.json'))
    assert len(requests) == 1
    task = json.loads(requests[0].read_text())
    assert task['operation'] == 'sync' and task['args']['categories'] == ['subscribed']
    assert 'url' not in task['args'] and 'id' not in task['args']
    checkpoint=workspace.session/'worker/account-export.json'
    original_checkpoint=checkpoint.read_bytes()
    value=json.loads(original_checkpoint)
    value['started_at']=(datetime.now(timezone.utc)-timedelta(seconds=7205)).isoformat()
    private_json(checkpoint,value)
    registry.working_since-=7205  # As if this program had been at work for two hours.
    with Inventory(workspace.library) as inventory:run=inventory.start_run()
    progress=workspace.library/'.archive-state/progress.json'
    private_json(progress,{'run_id':run,'status':'running','phase':'account_list','category':'subscribed',
        'reading_list_page_count':3,'observed_library_series_count':120,'message':'PRIVATE_COOKIE'})
    page.evaluate('poll()')
    expect(page.locator('#account-live-progress')).to_be_visible()
    expect(page.locator('#account-status')).to_contain_text('Subscribed list (1 of 4)')
    expect(page.locator('#account-list-count')).to_contain_text('3 pages read · 120 series listed')
    expect(page.locator('#account-save-title')).to_have_text('Finding your series')
    expect(page.locator('#dashboard-state')).to_have_text('Finding series')
    if screenshot:
        page.screenshot(path=str(screenshot.with_name(screenshot.stem+'-running.png')),full_page=True)
    expect(page.locator('#account-list-meter')).to_have_attribute('value','0')
    expect(page.locator('#account-clock')).to_contain_text('Last recorded activity')
    expect(page.locator('#account-list-help')).to_contain_text('several minutes')
    # How long is left: honest before a pace is known, then a rough length; nothing once nothing is waiting.
    known=registry.status(LOCAL_OWNER,workspace.id)
    for estimate,text in (({'waiting_episodes':426,'unchecked_series':610,'seconds':None},'Working out how long this will take'),
                          ({'waiting_episodes':426,'unchecked_series':610,'seconds':1814400},
                           'About 3 weeks of downloading left at the pace of the last five minutes. 426 episodes are waiting and 610 series have not been checked yet, so this is a rough guess. It only continues while this app is open.'),
                          ({'waiting_episodes':1,'unchecked_series':0,'seconds':5400},'About 2 hours of downloading left at the pace of the last five minutes. 1 episode is waiting.')):
        with patch.object(registry,'status',return_value=known|{'estimate':estimate}):
            page.evaluate('poll()')
            expect(page.locator('#account-estimate')).to_contain_text(text)
    with patch.object(registry,'status',return_value=known|{'estimate':{'waiting_episodes':0,'unchecked_series':0,'seconds':None}}):
        page.evaluate('poll()')
        expect(page.locator('#account-estimate')).to_be_hidden()
    # How much room: the whole library, what of it is saved, and whether this disk has it.
    left={'waiting_episodes':426,'unchecked_series':0,'seconds':None}
    for estimate,text in ((left|{'saved_bytes':3_040_000_000,'left_bytes':1_780_000_000_000,'unchecked_series':610,'free_bytes':412_000_000_000},
                           'Your whole library should take about 1.8 TB of space: 3.0 GB is saved and about 1.8 TB is still to come. This is a rough guess until every series has been checked. This disk has only 412 GB free, which is not enough for the rest.'),
                          (left|{'saved_bytes':0,'left_bytes':73_400_000_000,'free_bytes':73_400_000_000},'Your whole library should take about 73 GB of space. This disk has 73 GB free.'),
                          (left|{'saved_bytes':850_000_000,'left_bytes':400_000,'free_bytes':None},'Your whole library should take about 850 MB of space: 850 MB is saved and about 1 MB is still to come.')):
        with patch.object(registry,'status',return_value=known|{'estimate':estimate}):
            page.evaluate('poll()')
            expect(page.locator('#account-space')).to_have_text(text)
    with patch.object(registry,'status',return_value=known|{'estimate':{'waiting_episodes':0,'unchecked_series':0,'seconds':None,'saved_bytes':5,'left_bytes':0}}):
        page.evaluate('poll()')
        expect(page.locator('#account-space')).to_be_hidden()
    page.evaluate('poll()')
    old=time.time()-70;os.utime(progress,(old,old))
    page.evaluate('poll()')
    expect(page.locator('#account-activity')).to_contain_text('No new progress')
    expect(page.locator('#account-save-title')).to_have_text('Waiting for a progress update')
    expect(page.locator('#dashboard-state')).to_have_text('Waiting')
    expect(page.locator('#account-clock')).to_contain_text('Elapsed: 2h 0m')
    # Not a stall: the download waiting, by its own plan, to try again; and the moment between two steps.
    quiet={key:value for key,value in registry.account_status(LOCAL_OWNER,workspace.id).items() if key!='progress_age_seconds'}
    for extra,text in (({'retry':{'reason':'connection','seconds':70}},'The connection to Tapas was interrupted. The download tries again by itself in about a minute.'),
                       ({'retry':{'reason':'source','seconds':600}},'Tapas asked the app to wait. The download continues by itself in about 10 minutes.'),
                       ({'between_steps_seconds':4},'Working.')):
        with patch.object(registry,'account_status',return_value=quiet|extra):
            page.evaluate('poll()')
            expect(page.locator('#account-activity')).to_contain_text(text)
            expect(page.locator('#dashboard-state')).not_to_have_text('Waiting')
    with patch.object(registry,'account_status',return_value=quiet|{'between_steps_seconds':75}):
        page.evaluate('poll()')
        expect(page.locator('#account-activity')).to_contain_text('No new progress')
    page.evaluate('poll()')
    expect(page.locator('#account-activity')).to_contain_text('No new progress')
    private_json(progress,{'run_id':run,'status':'running','phase':'account_list','category':'subscribed',
        'reading_list_page_count':4,'observed_library_series_count':160})
    page.evaluate('poll()')
    expect(page.locator('#account-list-count')).to_contain_text('4 pages read · 160 series listed')
    expect(page.locator('#account-activity')).to_contain_text('Working.')
    route_pattern='**/api/v1/workspaces/'+workspace.id+'/account'
    def unavailable(route):
        route.fulfill(status=503,content_type='application/json',body='{"code":"request_unavailable"}')
    page.route(route_pattern,unavailable)
    page.evaluate('poll()')
    expect(page.locator('#account-activity')).to_contain_text('what it is doing right now is unknown')
    expect(page.locator('#dashboard-state')).to_have_text('Reconnecting')
    expect(page.locator('#dashboard-worker')).to_have_text('Downloader · Status unknown')
    expect(page.locator('#account-list-count')).to_contain_text('4 pages read')
    page.unroute(route_pattern,unavailable)
    page.evaluate('poll()')
    expect(page.locator('#account-activity')).to_contain_text('Working.')
    assert 'PRIVATE_COOKIE' not in page.locator('#account-download').inner_text()
    page.reload()
    expect(page.locator('#account-status')).to_contain_text('Finding series')
    expect(page.locator('#account-list-count')).to_contain_text('4 pages read')
    expect(page.locator('#account-go')).to_be_disabled()
    with Inventory(workspace.library) as inventory:inventory.finish_run(run,'finished',{})
    progress.unlink();checkpoint.write_bytes(original_checkpoint)
    page.locator('#account-pause').click()
    expect(page.locator('#account-status')).to_contain_text('Paused. Hit Go')
    expect(page.locator('#dashboard-state')).to_have_text('Paused')
    # A speed chosen in Settings while paused is kept, and Go continues the saved download at it.
    page.evaluate("location.hash = 'settings/downloads'")
    page.locator('#account-speed').select_option('slow')
    expect(page.locator('#account-speed-help')).to_contain_text('About one image a second')
    page.locator('#settings-back').click()
    page.locator('#account-go').click()
    expect(page.locator('#account-status')).to_contain_text('Finding series')
    expect(page.locator('#account-speed')).to_have_value('slow')
    budgets=json.loads(checkpoint.read_text())['window_budgets']
    assert (budgets['speed'],budgets['download_workers'],budgets['request_interval']) == ('normal',1,1.0), budgets
    assert list((workspace.session/'worker/requests').glob('*.json')) == requests
    page.locator('#account-pause').click()
    expect(page.locator('#account-status')).to_contain_text('Paused. Hit Go')
    requests[0].unlink()
    # Synthetic post-pause hold proves browser-only repair of expired login.
    checkpoint=workspace.session/'worker/account-export.json'
    # A hold for an unreachable site says so, and does not send the person looking for disk space.
    value=json.loads(checkpoint.read_text());value.update(status='held',code='network_unavailable')
    private_json(checkpoint,value)
    page.evaluate('poll()')
    expect(page.locator('#account-status')).to_contain_text('Tapas could not be reached')
    expect(page.locator('#account-status')).not_to_contain_text('disk space')
    held_card_messages(page)
    value.update(code='login_required')
    private_json(checkpoint,value)
    page.evaluate('poll()')
    expect(page.locator('#site-signin')).to_be_visible()
    expect(page.locator('#account-go')).to_be_disabled()
    class ScriptedSignIn:
        """The desktop window's site sign-in, reduced to the state this page reads."""
        _state={'status':'idle'}
        def status(self):return dict(self._state)
        def start(self):return dict(self._state)
        def close(self):pass
    signin=registry.site_signin=ScriptedSignIn()
    with patch.object(signin,'start',return_value={'status':'running'}):
        signin._state={'status':'running'}
        page.locator('#site-signin').click()
        expect(page.locator('#site-signin-status')).to_contain_text('detect it automatically')
        expect(page.locator('#account-save-title')).to_have_text('Finish signing in')
        expect(page.locator('#account-go')).to_be_disabled()
        signin._state={'status':'connected'}
        page.evaluate('poll()')
        expect(page.locator('#account-go')).to_be_enabled()
    checkpoint.unlink()
    signin._state={'status':'idle'}
    page.reload()
    expect(page.locator('#account-status')).to_contain_text('Ready. Hit Go')
    base='/api/v1/workspaces/'+workspace.id+'/account'
    def unavailable(route):
        route.fulfill(status=503,content_type='application/json',body='{"code":"request_unavailable"}')
    page.route('**'+base,unavailable)
    page.evaluate('poll()')
    expect(page.locator('#site-signin-status')).to_contain_text('retry automatically')
    page.unroute('**'+base,unavailable)
    page.evaluate('poll()')
    expect(page.locator('#site-signin-status')).to_have_text('')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if screenshot:
        page.screenshot(path=str(screenshot.with_name(screenshot.stem+'-simple.png')),full_page=True)


def saved_library_controls(context, site, registry, workspace, screenshot):
    """Browse beyond the first page using synthetic files and upload receipts."""
    from playwright.sync_api import expect
    import hashlib
    original = next(workspace.library.glob('*/*.cbz'))
    sidecar = original.with_suffix('.json')
    metadata = json.loads(sidecar.read_text())
    for number in range(1,75):
        archive = original.parent/f'sample-{number:03}.cbz'
        shutil.copy2(original, archive)
        value = metadata | {'export_id':str(uuid.uuid4()), 'title':f'Chapter {number}',
                            'imported_at':f'2025-01-{number%28+1:02}T14:30:00+00:00'}
        if number == 73: value['title'] = '<img src=x onerror=window.executed=1>'
        archive.with_suffix('.json').write_text(json.dumps(value))
    page = context.new_page()
    try:
        page.goto(site+'/library#'+workspace.id+'/all')
        expect(page.locator('html')).to_have_attribute('data-theme','dark')
        expect(page.locator('#theme-toggle')).to_have_text('')
        expect(page.locator('#theme-toggle')).to_have_attribute('aria-label','Switch to light mode')
        expect(page.locator('#theme-toggle .theme-sun')).to_be_visible()
        page.locator('#theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme','light')
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme','light')
        page.locator('#theme-toggle').click()
        expect(page.locator('html')).to_have_attribute('data-theme','dark')
        expect(page.locator('#notice')).to_have_text('Saved files loaded.')
        # The site is chosen in one place, the dashboard; the library page lists the open site's files, and names it.
        expect(page.locator('#site')).to_have_count(0)
        expect(page.locator('#library-title')).to_have_text('Your saved Tapas library')
        # Only the app's own window can open its save folder.
        expect(page.locator('#open-folder')).to_be_hidden()
        # Comics and novels are listed first: each episode once, not again for its data file.
        expect(page.locator('#file-kind')).to_have_value('episodes')
        page.locator('#refresh-files').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–50 of 75 files.')
        expect(page.locator('#files tr')).to_have_count(50)
        expect(page.locator('#files tr td:nth-child(4)').first).to_have_text('Comic')
        # Nothing is in cloud storage yet, so there is no storage column and the summary does not mention it.
        expect(page.locator('#library-summary')).not_to_contain_text('cloud')
        expect(page.locator('#storage-heading')).to_be_hidden()
        assert page.locator('#files td:nth-child(7)').evaluate_all('cells => cells.every(cell => cell.hidden)')
        assert page.evaluate('[sizes(1536), sizes(3*1024*1024)]') == ['1.5 KB', '3.0 MB']
        expect(page.locator('#files a')).to_have_count(0)
        assert page.locator('#files .file-action').evaluate_all('nodes => nodes.every(n => n.tagName === "BUTTON" && n.textContent === "Open")')
        row=page.locator('#files tr').filter(has_text='.cbz').first
        button=row.locator('.file-action');feedback=row.locator('.file-action-status')
        downloads=[];page.on('download',lambda value:downloads.append(value))
        with patch('comic_archive.local_web.subprocess.Popen',side_effect=FileNotFoundError('PRIVATE_HELPER_ERROR')):
            button.click()
            expect(feedback).to_contain_text('Choose a default app')
            expect(button).to_be_enabled()
            expect(button).to_have_text('Open')
            assert 'PRIVATE_HELPER_ERROR' not in page.locator('body').inner_text()
        with patch('comic_archive.local_web.subprocess.Popen') as launch:
            launch.return_value.wait.return_value=0
            button.click()
            expect(feedback).to_have_text('Sent to your device’s default app.')
            expect(button).to_have_attribute('aria-busy','false')
            launch.assert_called_once()
            assert launch.call_args.args[0][0]=='xdg-open'
            assert Path(launch.call_args.args[0][1]).parent==original.parent
        assert not downloads, 'Opening a device file must not create another downloaded copy'
        assert page.locator('#files img').count() == 0 and not page.evaluate('Boolean(window.executed)')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#file-kind').select_option('all')
        expect(page.locator('#file-range')).to_have_text('Showing 1–50 of 151 files.')
        data = page.locator('#files tr').filter(has_text='.json')
        expect(data.first.locator('td').nth(3)).to_have_text('Data file')
        # Only comics and novels are offered to open: a reading app opens those, not their data files.
        expect(data.locator('.file-action')).to_have_count(0)
        assert page.locator('#files tr').filter(has_text='.cbz').locator('.file-action').count() > 0
        page.locator('#next-page').click()
        expect(page.locator('#file-range')).to_have_text('Showing 51–100 of 151 files.')
        page.locator('#file-page').fill('4');page.locator('#go-page').click()
        expect(page.locator('#file-range')).to_have_text('Showing 151–151 of 151 files.')
        expect(page.locator('#next-page')).to_be_disabled()
        page.locator('#previous-page').click()
        expect(page.locator('#file-range')).to_have_text('Showing 101–150 of 151 files.')
        page.locator('#file-search').fill('Chapter 74');page.locator('#search-files').click()
        expect(page.locator('#files tr')).to_have_count(2)
        expect(page.locator('#file-range')).to_have_text('Showing 1–2 of 2 files.')
        expect(page.locator('#files time').first).to_have_attribute('datetime','2025-01-19T14:30:00+00:00')
        page.locator('#file-kind').select_option('episodes')
        expect(page.locator('#files tr')).to_have_count(1)
        page.reload();expect(page.locator('#files tr')).to_have_count(1)
        expect(page.locator('#file-search')).to_have_value('Chapter 74')
        expect(page.locator('#file-kind')).to_have_value('episodes')
        page.locator('#clear-search').click()
        expect(page.locator('#files tr')).to_have_count(50)
        expect(page.locator('#file-kind')).to_have_value('episodes')
        expect(page.locator('#library-locations [data-location=all]')).to_have_attribute('aria-pressed','true')
        page.locator('#page-size').select_option('100')
        expect(page.locator('#file-range')).to_have_text('Showing 1–75 of 75 files.')
        page.locator('#file-search').fill('NOT_SAVED_ANYWHERE');page.locator('#search-files').click()
        expect(page.locator('#file-range')).to_have_text('No saved files match these filters.')
        expect(page.locator('#files tr')).to_have_count(0)
        page.locator('#clear-search').click()
        expect(page.locator('#files tr')).to_have_count(75)
        page.locator('#file-sort').select_option('saved_asc')
        expect(page.locator('#notice')).to_have_text('Saved files loaded.')
        stamps = page.locator('#files time').evaluate_all('nodes => nodes.map(n => n.dateTime)')
        assert stamps == sorted(stamps)
        page.locator('#library-locations [data-location=device]').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–75 of 75 files.')
        from comic_archive.publication import public_metadata
        # The older Store fixture uses source metadata. Upload receipts
        # require the same neutral public metadata as real cloud exports.
        metadata = public_metadata(metadata, str(uuid.uuid4())) | {'format':'cbz','content_type':'comic'}
        sidecar.write_text(json.dumps(metadata))
        public_series = public_metadata(json.loads((original.parent/'series.json').read_text()), str(uuid.uuid4()), series=True)
        (original.parent/'series.json').write_text(json.dumps(public_series))
        state = workspace.library/'.archive-state/cloud';(state/'outbox/b2').mkdir(parents=True,exist_ok=True)
        library_id,target_id=str(uuid.uuid4()),str(uuid.uuid4())
        private_json(state/'targets.json',{'schema_version':1,'library_id':library_id,'targets':{'b2':{
            'remote':'b2','config':'/PRIVATE_CONFIG','target_id':target_id,'enabled':False,'tested':False}}})
        files = [{'name':name,'path':path.relative_to(workspace.library).as_posix(),
                  'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                 for name,path in [('episode.cbz',original),('episode.json',sidecar),('series.json',original.parent/'series.json')]]
        private_json(state/'outbox/b2'/(metadata['export_id']+'.json'),{
            'state':'verified','library_id':library_id,'target_id':target_id,'archive':files[0]['path'],
            'episode_id':metadata['export_id'],'series_id':json.loads((original.parent/'series.json').read_text())['export_id'],
            'files':files,'version':hashlib.sha256((files[0]['sha256']+files[1]['sha256']).encode()).hexdigest(),
            'verified_at':'2026-10-11T12:00:00+00:00','eviction_requested':True,'token':'PRIVATE_TOKEN'})
        original.unlink();page.locator('#refresh-files').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–74 of 74 files.')
        # All, this device and B2, which is set up: cloud storage that is not set up is not listed.
        expect(page.locator('#library-locations button')).to_have_count(3)
        expect(page.locator('#library-summary')).to_contain_text('153 files and cloud copies')
        expect(page.locator('#storage-heading')).to_be_visible()
        page.goto(site+'/library#'+workspace.id+'/b2?kind=all'); page.reload()
        expect(page.locator('#files tr')).to_have_count(3)
        expect(page.locator('#files td:nth-child(8)').first).to_have_text('Cloud copy')
        assert '/b2?' in page.url
        expect(page.locator('#notice')).to_have_text('Saved files loaded.')
        page.locator('#library-locations [data-location=b2]').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–3 of 3 files.')
        expect(page.locator('#files a')).to_have_count(0)
        expect(page.locator('#files .file-action')).to_have_count(0)
        expect(page.locator('#files time').first).to_have_attribute('datetime','2026-10-11T12:00:00+00:00')
        assert 'PRIVATE' not in page.locator('body').inner_text()
        page.reload()
        expect(page.locator('#library-locations [data-location=b2]')).to_have_attribute('aria-pressed','true')
        expect(page.locator('#files tr')).to_have_count(3)
        page.reload()
        expect(page.locator('#library-locations button')).to_have_count(3)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if screenshot:
            page.screenshot(path=str(screenshot.with_name(screenshot.stem+'-library.png')), full_page=True)
        # With nothing saved, the page says so once, and not also that nothing matches the filters.
        empty = {'locations':[{'id':'device','label':'This device','connected':True,'files':0,'episodes':0,'bytes':0}],
                 'episode_count':0, 'file_count':0, 'updated_at':'2026-10-11T12:00:00+00:00',
                 'files':[], 'total':0, 'offset':0, 'limit':50}
        nothing = lambda route: route.fulfill(status=200, content_type='application/json', body=json.dumps(empty))
        page.route(re.compile(r'/saved-files\?'), nothing)
        page.reload()
        expect(page.locator('#notice')).to_have_text(re.compile(r'^No files have been saved yet\. Use .+ to start\.$'))
        expect(page.locator('#file-range')).to_have_text('')
        page.unroute(re.compile(r'/saved-files\?'), nothing)
    finally:
        page.close()


def own_window_controls(context, site, registry, workspace):
    """Inside the desktop app's window the pages name its folder, keep the theme in the app and offer saving to B2."""
    from playwright.sync_api import expect
    asked, kept = [], ['system']
    class Location:
        def get(self): return '/home/user/Comic Archiver/Library'
        def ask(self, action): asked.append(action)
    class Theme:
        def get(self): return kept[-1]
        def set(self, value): kept.append(value)
    registry.location, registry.theme = Location(), Theme()
    page = context.new_page()
    try:
        for address in (site+'/', site+'/library#'+workspace.id+'/all'):
            page.goto(address)
            if address.endswith('/'):
                # The dashboard says where new episodes go, and leads to Settings to change it.
                expect(page.locator('#save-summary')).to_be_visible()
                expect(page.locator('#save-summary a')).to_have_attribute('href', '#settings/storage')
                wait_for_ui_idle(page)
                expect(page.locator('#save-summary-place')).to_have_text(re.compile(r'^(/home/user/Comic Archiver/Library|Backblaze B2)$'))
                # Settings opens at its first tab. The folder is on the Storage tab, in the card for this device.
                page.locator('#settings-nav').click()
                expect(page.locator('#downloads-settings')).to_be_visible()
                page.locator('#settings-tab-storage').click()
                expect(page.locator('#storage-page')).to_be_visible()
                expect(page.locator('#storage-device #save-location')).to_be_visible()
                expect(page.locator('#save-folder')).to_have_text('/home/user/Comic Archiver/Library')
                expect(page.locator('#change-folder')).to_be_visible()
                # B2 was connected earlier in this run: its card leads to what it holds, and to Backblaze itself.
                expect(page.locator('#storage-b2-files')).to_have_attribute('href', '/library#'+workspace.id+'/b2')
                expect(page.locator('#storage-b2-open')).to_have_attribute('href', 'https://secure.backblaze.com/b2_browse_files2.htm')
                expect(page.locator('#storage-b2-setup')).to_have_text('B2 settings')
                page.locator('#storage-b2-setup').click()
                expect(page.locator('#cloud-options')).to_be_visible()
                # B2 was connected earlier in this run, so its setup steps are folded until asked for.
                expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(2)
                page.locator('#b2-steps-toggle').click()
                expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(6)
                expect(page.locator('#b2-login')).to_be_visible()
                # The person is signed in from creating the bucket, so the link opens the keys page itself.
                expect(page.locator('#cloud-options .cloud-steps a[href*="app_keys"]')).to_have_attribute(
                    'href', 'https://secure.backblaze.com/app_keys.htm')
                page.locator('#settings-tab-storage').click()
                # The window keeps where episodes go in one place: the Storage tab, with the save folder.
                assert page.evaluate("document.getElementById('cloud-choice').parentElement.id") == 'storage-page'
                with page.expect_response(lambda response: response.url.endswith('/api/v1/location')) as sent:
                    page.locator('#open-folder').click()
                assert sent.value.status == 200 and asked == ['open']
            else:
                expect(page.locator('#library-locations [data-location=all]')).to_be_visible()
                expect(page.locator('#library-locations [data-location=device]')).to_be_visible()
                expect(page.locator('#notice')).to_have_text('Saved files loaded.')
                # The library page opens the same folder, the same way.
                with page.expect_response(lambda response: response.url.endswith('/api/v1/location')) as sent:
                    page.locator('#open-folder').click()
                assert sent.value.status == 200 and asked == ['open', 'open']
                expect(page.locator('#notice')).to_have_text('Your save folder opens in its own window.')
            wanted = 'light' if page.locator('html').get_attribute('data-theme') == 'dark' else 'dark'
            with page.expect_response(lambda response: response.url.endswith('/api/v1/theme')) as sent:
                page.locator('#theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme', wanted)
            assert sent.value.status == 200 and kept[-1] == wanted
        # Where the cloud helper is not offered (a device it has no pinned build for) neither page shows cloud saving.
        with patch('comic_archive.cloud_tools.platform.machine', return_value='riscv64'):
            page.goto(site+'/')
            wait_for_ui_idle(page)
            expect(page.locator('#save-summary-place')).to_have_text('/home/user/Comic Archiver/Library')
            expect(page.locator('#cloud-entry')).to_be_hidden()
            # Settings keeps its other tabs; B2's is not offered, and its address opens the first tab instead.
            page.evaluate("location.hash = 'settings/b2'")
            expect(page.locator('#downloads-settings')).to_be_visible()
            expect(page.locator('.settings-tabs a:visible')).to_have_text(['Downloads', 'Storage'])
            expect(page.locator('#cloud-options')).to_be_hidden()
            page.goto(site+'/library#'+workspace.id+'/all')
            expect(page.locator('#settings-nav')).to_be_visible()
            expect(page.locator('#library-locations [data-location=device]')).to_be_visible()
            expect(page.locator('#library-locations [data-location=all]')).to_have_count(0)
        # Choosing another site on the dashboard leaves the window a request to open it. The window switches
        # behind the page, which waits, saying so, and then reads the other site without being reloaded.
        from comic_archive import app_runtime, sites
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        listed, registry.site_choice = registry.sites, app_runtime.SiteChoice()
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            for address, said in ((site+'/', '#site-signin-status'),):
                registry.sites = sites.choices()
                page.goto(address)
                expect(page.locator('#site')).to_have_value('tapas')
                # The list is at the top of the status card, above the status it is for.
                assert page.locator('#site').bounding_box()['y'] < page.locator('#account-save-title').bounding_box()['y']
                expect(page.locator('#account-download #site')).to_have_count(1)
                if address.endswith('/'):
                    wait_for_ui_idle(page)
                    expect(page.locator('#site')).to_be_enabled()
                page.evaluate('window.notReloaded = true')
                # Switching asks first, and says the open site's episodes stay; saying no keeps that site.
                asked=[]
                page.once('dialog', lambda dialog: (asked.append(dialog.message), dialog.dismiss()))
                page.locator('#site').select_option('example')
                expect(page.locator('#site')).to_have_value('tapas')
                assert asked == ['Switch to Example? Your Tapas episodes stay saved, and you can switch back any time.'], asked
                page.once('dialog', lambda dialog: dialog.accept())
                with page.expect_response(lambda response: response.url.endswith('/api/v1/site')) as sent:
                    page.locator('#site').select_option('example')
                assert sent.value.status == 200 and registry.site_choice.take() == example
                expect(page.locator(said)).to_have_text('Switching to Example…')
                registry.sites = sites.choices(example); registry.site_choice.settle()
                expect(page.locator('#site')).to_have_value('example')
                expect(page.locator(said)).not_to_have_text('Switching to Example…')
                assert page.evaluate('window.notReloaded') is True
                if address.endswith('/'):
                    # With another site open, the account card and its badge name that site.
                    expect(page.locator('#dashboard-session')).to_have_text(re.compile(r'^Example · '))
                    expect(page.locator('#site-signin')).to_have_text('Sign in to Example')
            # The library page names the site whose files it lists.
            page.goto(site+'/library#'+workspace.id+'/all')
            expect(page.locator('#library-title')).to_have_text('Your saved Example library')
        registry.sites = listed
    finally:
        page.close()
        registry.location = registry.theme = registry.site_choice = None


def mobile_data_controls(page, registry, workspace):
    """On the Downloads tab of Settings, the mobile data choice says what the connection allows, and applies when
    ticked; when the connection holds saving, the dashboard says so and leads there."""
    from playwright.sync_api import expect
    def wifi_only(): return registry.network_status(LOCAL_OWNER, workspace.id)['wifi_only']
    def held_message():
        # One step, so a poll cannot replace the paused account between setting and reading it.
        return page.evaluate("""() => {
            const account = state.account, status = state.status;
            state.account = {...account, status:'held', code:'task_failed'};
            state.status = {...status, session_configured:true, worker:{reported_state:'ready'}};
            renderAccount(); const text = $('account-status').textContent;
            state.account = account; state.status = status; renderAccount(); return text;}""")
    settings_link = lambda: page.evaluate("!document.getElementById('account-settings-link').hidden")
    assert 'mobile data' not in held_message() and not settings_link()
    page.evaluate("location.hash = 'settings/downloads'")
    expect(page.locator('#account-network')).to_be_visible()
    expect(page.locator('#account-mobile-data')).not_to_be_checked()
    expect(page.locator('#account-network-help')).to_contain_text('Off: saving uses Wi-Fi or a wired connection')
    for probe, reason in (({'connection':'cellular', 'metered':True}, 'This connection is metered'),
                          ({'connection':'unknown', 'metered':None}, 'cannot tell')):
        with patch('comic_archive.network.probe', return_value=probe):
            page.evaluate('network(false)')
            expect(page.locator('#account-network-help')).to_contain_text(reason)
            expect(page.locator('#account-mobile-data')).not_to_be_checked()
            assert 'press Allow this connection, then hit Go' in held_message()
            # The way out is beside the message, not in Settings.
            assert not settings_link()
    expect(page.locator('#account-settings-link a')).to_have_attribute('href', '#settings/downloads')
    with patch('comic_archive.network.probe', return_value={'connection':'cellular', 'metered':True}):
        page.evaluate('network(false)')
        with page.expect_response(lambda response: response.url.endswith('/network') and response.request.method == 'POST'):
            page.locator('#account-mobile-data').check()
        expect(page.locator('#account-network-help')).to_contain_text('any connection')
        assert wifi_only() is False
        assert 'mobile data' not in held_message() and not settings_link()
        with page.expect_response(lambda response: response.url.endswith('/network') and response.request.method == 'POST'):
            page.locator('#account-mobile-data').uncheck()
        assert wifi_only() is True
        expect(page.locator('#account-network-help')).to_contain_text('This connection is metered')
    page.evaluate('network(false)')
    expect(page.locator('#account-network-help')).to_contain_text('Off: saving uses Wi-Fi or a wired connection')
    assert not settings_link()
    page.locator('#settings-back').click()
    expect(page.locator('#account-download')).to_be_visible()
    expect(page.locator('#account-allow-network')).to_be_hidden()
    # On a connection that holds saving, Allow this connection is beside the message and lets it through.
    with patch('comic_archive.network.probe', return_value={'connection':'unknown', 'metered':None}):
        page.evaluate('network(false)')
        expect(page.locator('#account-allow-network')).to_be_visible()
        assert 'a VPN can cause this' in held_message()
        with page.expect_response(lambda response: response.url.endswith('/network') and response.request.method == 'POST'):
            page.locator('#account-allow-network').click()
        expect(page.locator('#account-allow-network')).to_be_hidden()
        assert wifi_only() is False
        page.evaluate("api(base()+'/network', {wifi_only:true}).then(() => network(false))")
        expect(page.locator('#account-allow-network')).to_be_visible()
    page.evaluate('network(false)')
    expect(page.locator('#account-allow-network')).to_be_hidden()
    assert wifi_only() is True


def site_settings_controls(page, registry, workspace):
    """Settings lists the speed of each site there is a sign-in for, and one Backblaze setup serves every site
    unless the open one keeps its own."""
    from playwright.sync_api import expect
    from comic_archive import shared
    class Sites:
        speed, chosen = 'fast', []
        def list(self):
            return [{'key':'tapas', 'name':'Tapas', 'current':True, 'speed':'fast'},
                    {'key':'tappytoon', 'name':'Tappytoon', 'current':False, 'speed':self.speed}]
        def choose_speed(self, key, value):
            self.chosen.append((key, value)); self.speed = value
            return self.list()
    others = registry.site_settings = Sites()
    try:
        page.reload()
        page.evaluate("location.hash = 'settings/downloads'")
        expect(page.locator('#account-speed-label')).to_have_text('Speed on Tapas')
        row = page.locator('#other-site-speeds .setting')
        expect(row).to_have_count(1)
        expect(row.locator('label')).to_have_text('Speed on Tappytoon')
        expect(row.locator('select')).to_have_value('fast')
        expect(row.locator('p')).to_have_text('Used the next time you hit Go on Tappytoon.')
        expect(page.locator('#downloads-signin-note')).to_be_hidden()
        with page.expect_response(lambda response: response.url.endswith('/api/v1/site-speed') and response.ok):
            row.locator('select').select_option('slow')
        assert others.chosen == [('tappytoon', 'slow')], others.chosen
        expect(row.locator('select')).to_have_value('slow')
        # A site that is not signed in has no speed to choose; with no other site either, Settings says how to get one.
        shown = page.evaluate("""() => {
            const status = state.status, listed = state.context.site_settings, seen = [];
            const look = () => seen.push([$('account-speed-row').hidden, $('downloads-signin-note').hidden,
                                          $('other-site-speeds').children.length, $('account-speed-label').textContent]);
            state.status = {...status, session_configured:false}; renderAccount(); look();
            state.context.site_settings = []; renderAccount(); look();
            state.status = status; renderAccount(); look();
            state.context.site_settings = listed; renderAccount(); return seen;}""")
        assert shown == [[True, True, 1, 'Speed on Tapas'], [True, False, 0, 'Speed'], [False, True, 0, 'Speed']], shown
    finally:
        registry.site_settings = None
    own = workspace.library / '.archive-state/cloud/targets.json'
    had = own.exists()
    page.evaluate("location.hash = 'settings/b2'")
    expect(page.locator('#b2-sharing')).to_be_hidden()
    shared.link(workspace.library, workspace.library.parent / '.comic-shared')
    try:
        page.evaluate('cloud()')
        expect(page.locator('#b2-sharing')).to_be_visible()
        expect(page.locator('#b2-shared')).to_be_checked()
        expect(page.locator('#b2-sharing-help')).to_have_text('Every site you save from uses it, each in a folder of its own in your bucket.')
        with page.expect_response(lambda response: response.url.endswith('/cloud/storage') and response.ok):
            page.locator('#b2-shared').uncheck()
        expect(page.locator('#b2-sharing-help')).to_have_text('Tapas has a Backblaze setup of its own. Your other sites keep the shared one.')
        assert shared.cloud_mode(workspace.library) == 'separate'
        with page.expect_response(lambda response: response.url.endswith('/cloud/storage') and response.ok):
            page.locator('#b2-shared').check()
        assert shared.cloud_mode(workspace.library) == 'shared'
    finally:
        (workspace.library / shared.LINK).unlink()
        if not had:
            own.unlink(missing_ok=True)
    page.evaluate('cloud()')
    expect(page.locator('#b2-sharing')).to_be_hidden()
    page.locator('#settings-back').click()
    expect(page.locator('#account-download')).to_be_visible()


def b2_setup_controls(page, registry):
    """Four-step default setup, private key clearing, nearby feedback and reload."""
    from playwright.sync_api import expect
    page.reload(); open_b2(page)
    expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(6)
    # The top of the page says whether B2 is set up.
    expect(page.locator('#b2-state-pill')).to_have_text('Not set up')
    expect(page.locator('#b2-steps-fold')).to_be_hidden()
    expect(page.locator('#b2-bucket-name')).to_have_value(re.compile(r'comic-library-[a-f0-9]{12}'))
    expect(page.locator('#b2-key-bucket')).to_have_text(page.locator('#b2-bucket-name').input_value())
    # Bucket names are unique across all of Backblaze: said plainly, right under the name, not in the small print.
    expect(page.locator('#b2-bucket-unique')).to_be_visible()
    expect(page.locator('#b2-bucket-unique b')).to_have_text('A bucket name can belong to only one Backblaze account, anywhere.')
    assert page.locator('label:has(#b2-bucket-name) + p').get_attribute('id') == 'b2-bucket-unique'
    # Backblaze refuses a key name with anything but letters, numbers and hyphens.
    assert re.fullmatch(r'[A-Za-z0-9-]{1,100}', page.locator('#b2-key-name').inner_text())
    # What Backblaze charges is offered before the first step, closed, with the estimate as its heading.
    expect(page.locator('#b2-costs > summary')).to_be_visible()
    expect(page.locator('#b2-costs > summary')).to_contain_text('Estimated Backblaze cost:')
    expect(page.locator('#b2-costs > summary')).to_contain_text('Cost details')
    assert page.locator('#b2-costs').bounding_box()['y'] < page.locator('#cloud-options .cloud-steps').bounding_box()['y']
    expect(page.locator('#b2-costs')).not_to_have_attribute('open', '')
    expect(page.locator('#b2-costs ul')).to_be_hidden()
    page.locator('#b2-costs > summary').click()
    expect(page.locator('#b2-costs ul')).to_be_visible()
    expect(page.locator('#b2-costs ul')).to_contain_text('first 10 GB you store is always free')
    for href in ('https://secure.backblaze.com/billing.htm', 'https://secure.backblaze.com/b2_caps_alerts.htm'):
        expect(page.locator(f'#cloud-options .cloud-steps > li:first-child a[href="{href}"]')).to_have_count(1)
    # The person's own library, priced: free up to 10 GB, then only for what is past it.
    cost = lambda saved, left, unchecked=0: page.evaluate(
        'e => b2Cost(e)', {'saved_bytes': saved, 'left_bytes': left, 'unchecked_series': unchecked})
    assert cost(0, 0) == ''
    assert 'fits in Backblaze’s free 10 GB' in cost(4 * 10**9, 6 * 10**9)
    # Backblaze's pages count by the day; people speak of a month. Both are given.
    assert 'about 2 cents a day, about 63 cents a month' in cost(60 * 10**9, 40 * 10**9), cost(60 * 10**9, 40 * 10**9)
    assert 'about 5 cents a day, about $1.60 a month' in cost(240 * 10**9, 0)
    assert 'charge less than 1 cent a day, about 7 cents a month' in cost(20 * 10**9, 0)
    assert 'about $1.16 a day, about $34.75 a month' in cost(5010 * 10**9, 0)
    assert 'rough guess' in cost(240 * 10**9, 0, unchecked=3) and 'rough guess' not in cost(240 * 10**9, 0)
    page.evaluate("state.status = {...(state.status || {}), estimate: {saved_bytes: 240e9, left_bytes: 0}}; renderCost()")
    expect(page.locator('#b2-cost-estimate')).to_be_visible()
    expect(page.locator('#b2-cost-estimate')).to_have_text(re.compile(r'about 240 GB\. Once all of it is stored, Backblaze would charge about 5 cents a day, about \$1\.60 a month\.'))
    expect(page.locator('#b2-cost-short')).to_have_text('about 5 cents a day, about $1.60 a month')
    page.evaluate("state.status = {...state.status, estimate: {saved_bytes: 4e9, left_bytes: 0}}; renderCost()")
    expect(page.locator('#b2-cost-short')).to_have_text('free, your library fits in the free 10 GB')
    page.evaluate("state.status = {...state.status, estimate: {saved_bytes: 0, left_bytes: 0}}; renderCost()")
    expect(page.locator('#b2-cost-estimate')).to_be_hidden()
    # Before the library's size is known, the heading prices 100 GB.
    expect(page.locator('#b2-cost-short')).to_have_text('free up to 10 GB, then about 2 cents a day, about 70 cents a month, for each 100 GB more')
    # Backblaze's own pages for afterwards, each with what it is for.
    for href in ('b2_browse_files2.htm', 'b2_reports.htm', 'billing.htm', 'b2_caps_alerts.htm', 'app_keys.htm'):
        expect(page.locator(f'#b2-pages a[href="https://secure.backblaze.com/{href}"]')).to_have_count(1)
    expect(page.locator('#b2-pages')).to_contain_text('ordinary CBZ files')
    # The heading closes the details again.
    page.locator('#b2-costs > summary').click()
    expect(page.locator('#b2-costs ul')).to_be_hidden()
    signin = registry.cloud_signin
    started = []
    fixture_key = 'a' * 8 + '+' + 'b' * 8 + '/' + 'c' * 8
    def start(provider, credentials):
        started.append((provider, dict(credentials)))
        signin._state = {'status':'running', 'provider':'b2', 'stage':'checking_b2'}
        return signin.status()
    with patch.object(signin, 'start', side_effect=start):
        # Keys are pasted as copied, with the space a copy often brings, which the fields' patterns would refuse.
        page.locator('#b2-key-id').fill(' fixtureKeyID12 ')
        page.locator('#b2-application-key').fill(' ' + fixture_key + ' ')
        # The bucket name is the person's to choose; one Backblaze would refuse is caught here first.
        page.locator('#b2-bucket-name').fill('lib')
        page.locator('#connect-b2').click()
        assert not started and not page.locator('#b2-bucket-name').evaluate('node => node.validity.valid')
        page.locator('#b2-bucket-name').fill(' My-Comics-2026 ')
        expect(page.locator('#b2-bucket-name')).to_have_value('My-Comics-2026')
        # The key step names the same bucket to choose.
        expect(page.locator('#b2-key-bucket')).to_have_text('My-Comics-2026')
        # A status refresh leaves what was typed alone.
        page.evaluate('cloud()')
        expect(page.locator('#b2-bucket-name')).to_have_value('My-Comics-2026')
        page.locator('#connect-b2').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Checking your B2 key')
        expect(page.locator('#b2-key-id')).to_have_value('fixtureKeyID12')
        expect(page.locator('#b2-application-key')).to_have_value(fixture_key)
        expect(page.locator('#connect-b2')).to_be_disabled()
        expect(page.locator('#b2-state-pill')).to_have_text('Checking your key')
        expect(page.locator('#cloud-auth-link')).to_be_hidden()
        assert started == [('b2', {'key_id':'fixtureKeyID12', 'application_key':fixture_key, 'bucket':'My-Comics-2026'})]
        assert fixture_key not in page.content()
        # A key for another bucket says so, and the typed name stays to be corrected.
        wait_for_ui_idle(page)
        signin._state = {'status':'error', 'provider':'b2', 'code':'b2_bucket_mismatch'}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('different bucket than the bucket name in step 2')
        expect(page.locator('#b2-bucket-name')).to_have_value('My-Comics-2026')
        # The keys stay to try again with, not to be pasted again.
        expect(page.locator('#b2-key-id')).to_have_value('fixtureKeyID12')
        expect(page.locator('#b2-application-key')).to_have_value(fixture_key)
        expect(page.locator('#connect-b2')).to_be_enabled()
        # Leaving the tab lets them go.
        page.locator('#settings-tab-storage').click(); open_b2(page)
        expect(page.locator('#b2-key-id')).to_have_value('')
        expect(page.locator('#b2-application-key')).to_have_value('')
        # So does a connection that works.
        page.locator('#b2-key-id').fill('fixtureKeyID12'); page.locator('#b2-application-key').fill(fixture_key)
        page.locator('#connect-b2').click()
        expect(page.locator('#connect-b2')).to_be_disabled()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Checking your B2 key')
        wait_for_ui_idle(page)
        signin._state = {'status':'connected', 'provider':'b2'}
        page.evaluate('poll()')
        expect(page.locator('#b2-application-key')).to_have_value('')
        expect(page.locator('#b2-key-id')).to_have_value('')
        signin._state = {'status':'running', 'provider':'b2', 'stage':'checking_b2'}
        page.reload(); open_b2(page)
        expect(page.locator('#cloud-auth-status')).to_contain_text('Checking your B2 key')
        expect(page.locator('#b2-application-key')).to_have_value('')
        signin._state = {'status':'error', 'provider':'b2', 'code':'b2_private_bucket'}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('Choose Private')
        expect(page.locator('#connect-b2')).to_be_enabled()
    signin._state = {'status':'idle'}
    page.reload(); open_b2(page)
    expect(page.locator('#b2-bucket-name')).to_have_value(re.compile(r'comic-library-[a-f0-9]{12}'))
    # Once connected, the page names the bucket the connection saved, and the dashboard's button opens it.
    original_status, tested, enabled, failed = registry.cloud_status, [], [], []
    def status(owner, workspace_id):
        value = original_status(owner, workspace_id)
        for target in value['targets']:
            if target['provider'] == 'b2':
                target.update(connected=True, tested=bool(tested), enabled=bool(enabled), failed=len(failed), bucket='My-Comics-2026')
        return value
    state = lambda: (page.locator('#b2-state-pill'), page.locator('#b2-state-text'))
    with patch.object(registry, 'cloud_status', side_effect=status):
        page.evaluate('cloud()')
        expect(page.locator('#b2-bucket-name')).to_have_value('My-Comics-2026')
        expect(state()[0]).to_have_text('Connected, not checked yet')
        expect(state()[1]).to_contain_text('Connected to your bucket My-Comics-2026. Check the connection (step 5)')
        # Once connected, the steps that did it fold away; what is left keeps its numbers.
        steps = page.locator('#cloud-options .cloud-steps > li:visible')
        expect(steps).to_have_count(2)
        expect(steps.first).to_contain_text('Check the connection')
        assert page.locator('#cloud-steps').evaluate('node => node.start') == 5
        toggle = page.locator('#b2-steps-toggle')
        expect(toggle).to_have_text('Show the setup steps')
        toggle.click()
        expect(steps).to_have_count(6)
        expect(toggle).to_have_text('Hide the setup steps')
        assert page.locator('#cloud-steps').evaluate('node => node.start') == 1
        toggle.click()
        expect(steps).to_have_count(2)
        # Where episodes go is chosen on the Storage tab: each storage that is set up, by its name alone. One not yet
        # checked is listed but cannot be chosen.
        page.locator('#cloud-done').click()
        expect(page.locator('#cloud-options')).to_be_hidden()
        expect(page.locator('#storage-page')).to_be_visible()
        choice = page.locator('#storage-options input')
        expect(page.locator('#cloud-choice')).to_be_visible()
        expect(page.locator('#storage-options label')).to_have_text(['This device', 'Backblaze B2'])
        expect(choice.nth(0)).to_be_checked()
        expect(choice.nth(1)).to_be_disabled()
        expect(page.locator('#cloud-choice-help')).to_contain_text('Backblaze B2 can be chosen once its connection check passes')
        expect(page.locator('#cloud-entry')).to_be_hidden()
        tested.append(True); page.evaluate('cloud()')
        open_b2(page)
        expect(state()[0]).to_have_text('Set up')
        expect(state()[1]).to_contain_text('Uploads are off; choose Backblaze B2 on the Storage tab')
        page.locator('#cloud-done').click()
        expect(choice.nth(1)).to_be_enabled()
        expect(page.locator('#cloud-choice')).not_to_contain_text('My-Comics-2026')
        # It is at the top of the Storage tab, before what each place holds.
        assert page.evaluate("document.getElementById('cloud-choice').parentElement.id") == 'storage-page'
        assert page.locator('#cloud-choice').bounding_box()['y'] < page.locator('.storage-cards').bounding_box()['y']
        # A refused choice says why and shows what is saved.
        with patch.object(registry, 'configure_cloud', side_effect=ArchiveError('Fixture refusal.')):
            choice.nth(1).click()
            expect(page.locator('#cloud-choice-status')).to_have_text(re.compile(r'.+'))
            expect(choice.nth(0)).to_be_checked()
        with patch.object(registry, 'configure_cloud', return_value={}) as configure:
            choice.nth(1).click()
            expect(page.locator('#cloud-choice-status')).to_have_text('')
            assert configure.call_args.args[2:] == ('b2', True)
        enabled.append(True); page.evaluate('cloud()')
        expect(choice.nth(1)).to_be_checked()
        expect(page.locator('#cloud-choice-help')).to_contain_text('uploaded to Backblaze B2 as it downloads')
        # Choosing this device again turns the uploads off.
        with patch.object(registry, 'configure_cloud', return_value={}) as configure, page.expect_response(
                lambda response: response.url.endswith('/cloud') and response.request.method == 'POST'):
            choice.nth(0).click()
        assert configure.call_args.args[2:] == ('b2', False)
        # The Storage tab's B2 card names the bucket and leads to what it holds and to Backblaze itself.
        page.evaluate("location.hash = 'settings/storage'")
        expect(page.locator('#storage-b2-pill')).to_have_text('Uploading to B2')
        expect(page.locator('#storage-b2-text')).to_contain_text('bucket My-Comics-2026')
        for selector in ('#storage-b2-files', '#storage-b2-open'):
            expect(page.locator(selector)).to_be_visible()
        expect(page.locator('#storage-b2-setup')).to_have_text('B2 settings')
        open_b2(page)
        expect(state()[0]).to_have_text('Uploading to B2')
        failed.append(1); page.evaluate('cloud()')
        expect(state()[0]).to_have_text('Uploads need attention')
        expect(state()[1]).to_contain_text('1 upload needs retrying: click Retry unfinished uploads below.')
    page.reload(); open_b2(page)


def open_b2(page):
    """B2 setup is a tab of Settings, at #settings/b2, in place of the dashboard."""
    from playwright.sync_api import expect
    page.evaluate("location.hash = 'settings/b2'")
    expect(page.locator('#cloud-options')).to_be_visible()
    expect(page.locator('#account-download')).to_be_hidden()


def leave_b2(page):
    """A fresh dashboard, as the app starts."""
    page.goto(page.url.split('#')[0])


def cloud_signin_controls(page, registry):
    """A B2 check in progress survives a reload, can be cancelled, and says how it ended, without provider traffic."""
    from playwright.sync_api import expect
    signin = registry.cloud_signin
    started = []
    connected = False
    original = registry.cloud_status
    fixture_key = 'd' * 8 + '+' + 'e' * 8
    def status(owner, workspace):
        value = original(owner, workspace)
        for target in value['targets']:
            if target['provider'] == 'b2': target['connected'] = connected
        return value
    def start(provider, credentials):
        started.append((provider, dict(credentials)))
        signin._state = {'status':'running', 'provider':provider, 'stage':'preparing'}
        return signin.status()
    def cancel():
        signin._state = {'status':'cancelled'}
        return signin.status()
    with patch.object(signin, 'start', side_effect=start), patch.object(signin, 'cancel', side_effect=cancel), patch.object(registry, 'cloud_status', side_effect=status):
        open_b2(page)
        expect(page.locator('#cancel-cloud-signin')).to_be_hidden()
        page.locator('#b2-key-id').fill('fixtureKeyID34'); page.locator('#b2-application-key').fill(fixture_key)
        page.locator('#connect-b2').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Preparing cloud sign-in')
        expect(page.locator('#cancel-cloud-signin')).to_be_visible()
        assert started[-1][0] == 'b2' and started[-1][1]['application_key'] == fixture_key
        assert fixture_key not in page.content()
        wait_for_ui_idle(page)
        signin._state = {'status':'running', 'provider':'b2', 'stage':'checking_b2'}
        page.reload(); open_b2(page)
        expect(page.locator('#cloud-auth-status')).to_contain_text('Checking your B2 key')
        expect(page.locator('#connect-b2')).to_be_disabled()
        page.locator('#cancel-cloud-signin').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('cancelled')
        expect(page.locator('#cancel-cloud-signin')).to_be_hidden()
        expect(page.locator('#connect-b2')).to_be_enabled()
        for code, message in [('signin_network_held', 'Mobile data is off'),
                              ('cloud_rate_limited', 'The provider limited requests'),
                              ('signin_failed', 'try again')]:
            wait_for_ui_idle(page)
            signin._state = {'status':'error', 'provider':'b2', 'code':code}
            page.evaluate('poll()')
            expect(page.locator('#cloud-auth-status')).to_contain_text(message)
        wait_for_ui_idle(page)
        connected = True; signin._state = {'status':'connected', 'provider':'b2'}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('Signed in. Next, click Test cloud connection.')
        expect(page.locator('#test-cloud')).to_be_enabled()
    signin._state = {'status':'idle'}
    leave_b2(page)


def wait_for_ui_idle(page):
    """Wait for synthetic requests using evaluate, without dynamic CSP eval."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if page.evaluate('() => !state.polling && !state.busy'):
            return
        page.wait_for_timeout(50)
    raise AssertionError('Synthetic UI did not finish the preceding request')


def cloud_controls(page, registry, workspace):
    """Exercise fixed local test/cleanup tasks with synthetic worker receipts."""
    from playwright.sync_api import expect
    original_status = registry.cloud_status
    tested = False
    failure_reasons = {}
    def status(owner, workspace_id):
        value = original_status(owner, workspace_id)
        for target in value['targets']:
            if target['provider'] == 'b2':
                target.update(connected=True, tested=tested, enabled=tested, failure_reasons=failure_reasons)
        return value
    queue = workspace.session/'worker'
    def complete(operation, result, *, error_code=None):
        requests = []
        for path in (queue/'requests').glob('*.json'):
            request = json.loads(path.read_text())
            if request['operation'] == operation:
                requests.append((path, request))
        assert len(requests) == 1, 'Expected one bounded cloud task'
        path, request = requests[0]
        private_json(queue/'responses'/path.name, {'task_id':path.stem,
            **({'status':'error','error_code':error_code,'error':'PRIVATE_TOKEN PRIVATE_PATH'} if error_code
               else {'status':'ok','result':result})})
        path.unlink()
        return request['args']
    with patch.object(registry, 'cloud_status', side_effect=status):
        page.reload()
        open_b2(page)
        expect(page.locator('#test-cloud')).to_be_enabled()
        page.locator('#test-cloud').click()
        try:expect(page.locator('#notice')).to_contain_text('Testing two completed episodes')
        except AssertionError:
            # Fixed UI error labels and booleans only; never log fixture data.
            print(json.dumps(page.evaluate('''({busy:state.busy,
                taskKind:state.taskKind,noticeHidden:document.getElementById('notice').hidden,
                errorCode:Object.entries(errors).find(([key,value]) => value === document.getElementById('notice').textContent)?.[0] || 'unknown'})''')))
            raise
        expect(page.locator('#test-cloud')).to_be_disabled()
        expect(page.locator('#cloud-action-status')).to_contain_text('Testing two completed episodes')
        expect(page.locator('#account-go')).to_be_disabled()
        assert complete('cloud_test', {'verified_episodes':2,'test_files_deleted':True,'local_archives_retained':True}) == {'provider':'b2'}
        tested = True
        test_id = 'f257c685-2a39-4e16-8dc0-353338e2f7d7'
        receipt = workspace.library/'.archive-state/cloud/tests'/f'{test_id}.json'
        receipt.parent.mkdir(parents=True, exist_ok=True)
        private_json(receipt, {'provider':'b2','state':'cleanup_pending','error_code':'cloud_login_required'})
        expect(page.locator('#notice')).to_contain_text('2 episodes verified', timeout=10000)
        expect(page.locator('#test-cloud')).to_be_disabled()
        cleanup = page.locator('#cloud-cleanup button')
        expect(cleanup).to_have_text('Retry Backblaze B2 test cleanup')
        expect(page.locator('#cloud-repair')).to_contain_text('Test cleanup pending')
        expect(page.locator('#cloud-repair')).to_contain_text('Cloud sign-in needs repair')
        wait_for_ui_idle(page)
        cleanup.click()
        try:
            expect(page.locator('#notice')).to_contain_text('exact recorded test files')
        except AssertionError:
            print(json.dumps(page.evaluate("""({cleanupFailure:true,busy:state.busy,
                polling:state.polling,taskKind:state.taskKind,
                errorCode:Object.entries(errors).find(([key,value]) => value === document.getElementById('notice').textContent)?.[0] || 'unknown'})""")))
            raise
        assert complete('cloud_cleanup', {'test_files_deleted':True}) == {'test_id':test_id}
        private_json(receipt, {'provider':'b2','state':'cleaned'})
        expect(page.locator('#notice')).to_contain_text('Recorded test copies deleted', timeout=10000)
        expect(page.locator('#cloud-cleanup button')).to_have_count(0)
        expect(page.locator('#test-cloud')).to_be_enabled()
        failure_reasons = {'cloud_space':1,'cloud_cap':2,'PRIVATE_TOKEN':999,'cloud_connection':True,'cloud_login_required':-1}
        page.evaluate('cloud()')
        expect(page.locator('#cloud-repair')).to_contain_text('insufficient cloud storage')
        # A Backblaze cap says which of its pages lifts it, not that the cloud is full.
        expect(page.locator('#cloud-repair')).to_contain_text('2 failed uploads: Backblaze stopped the upload: your account has used its free 10 GB')
        expect(page.locator('#cloud-repair')).not_to_contain_text('unavailable or timed out')
        page.locator('#retry-cloud').click()
        expect(page.locator('#notice')).to_contain_text('Retrying up to three pending episode uploads')
        assert complete('cloud_upload',{},error_code='cloud_space') == {}
        page.evaluate('poll()')
        expect(page.locator('#notice')).to_contain_text('insufficient cloud storage')
        expect(page.locator('#notice')).to_contain_text('Local episodes are retained')
        assert 'PRIVATE' not in page.locator('body').inner_text()
        failure_reasons = {}
        page.evaluate('cloud()')
        expect(page.locator('#cloud-repair')).to_have_text('')
    leave_b2(page)
    expect(page.locator('#notice')).to_have_text('Library loaded.')


def smoke(screenshot=None,mobile=False):
    from playwright.sync_api import sync_playwright,expect
    # Shared builders can delay durable fixture writes; retain bounded assertions.
    expect.set_options(timeout=10000)
    with tempfile.TemporaryDirectory(prefix='comic-dashboard-test-') as work, \
            patch('comic_archive.network.probe', return_value={'connection':'wired', 'metered':False}):
        project=Path(work)/'project';project.mkdir()
        registry=LocalWorkspaceRegistry(project)
        alice=registry.resolve(LOCAL_OWNER,registry.workspace_id)
        # Another project on the same computer has its own key, so its workspace is a stranger to this dashboard.
        other=Path(work)/'other-project';other.mkdir()
        others=LocalWorkspaceRegistry(other)
        bob=others.resolve(LOCAL_OWNER,others.workspace_id)
        assert bob.id!=alice.id
        title='<img src=x onerror=window.executed=1>'
        with Inventory(alice.library) as inventory:
            inventory.bind_account('11')
            inventory.observe('42',title,[{'id':'81','title':'First','order':1,'access_state':'tapas_unlocked'}],'fixture')
            inventory.observe('43','Another observed series',[],'fixture')
            inventory.record_series_scan('42')
        with Inventory(bob.library) as inventory:inventory.observe('99','BOB_PRIVATE',[],'fixture')
        source=Path(work)/'page.png';source.write_bytes(png())
        class LocalPages:
            def retrieve(self,page):return source.read_bytes()
        series=Series('sample','A portable sample',platform_id='42')
        episode=Episode('81','sample','First',1,(Page(1,str(source)),),'local_owned')
        store=Store(alice.library)
        try:store.archive(Manifest(series,(episode,)),LocalPages(),progress=lambda _:None)
        finally:store.close()
        with Inventory(alice.library) as inventory:inventory.reconcile()
        private_json(alice.session/'state.json',{'cookies':[],'origins':[]})
        (alice.session/'worker/requests').mkdir(parents=True)
        (alice.session/'worker/responses').mkdir()
        private_json(alice.session/'worker/status.json',{'state':'ready'})
        site='http://127.0.0.1:8765'
        app=LocalApplication(registry,8765)
        calls=[]

        def route_request(route):
            request=route.request;url=urlsplit(request.url)
            if url.scheme+'://'+url.netloc!=site:
                raise AssertionError('Unexpected external browser request')
            body=request.post_data_buffer or b''
            headers=request.all_headers()
            env={'REQUEST_METHOD':request.method,'PATH_INFO':url.path,'QUERY_STRING':url.query,
                 'CONTENT_TYPE':headers.get('content-type',''),'CONTENT_LENGTH':str(len(body)),
                 'HTTP_ORIGIN':headers.get('origin',''),'HTTP_X_CSRF_TOKEN':headers.get('x-csrf-token',''),
                 'HTTP_COOKIE':headers.get('cookie',''),'HTTP_HOST':url.netloc,'REMOTE_ADDR':'127.0.0.1',
                 'wsgi.url_scheme':url.scheme,'HTTP_SEC_FETCH_SITE':headers.get('sec-fetch-site',''),
                 'HTTP_REFERER':headers.get('referer',''),
                 'wsgi.input':io.BytesIO(body)}
            captured=[]
            result=app(env,lambda status,values,exc_info=None:captured.append((status,dict(values))))
            try:payload=b''.join(result)
            finally:
                if hasattr(result,'close'):result.close()
            status,values=captured[0]
            calls.append((request.method,url.path,int(status.split()[0])))
            route.fulfill(status=int(status.split()[0]),headers=values,body=payload)

        with sync_playwright() as playwright:
            browser=playwright.firefox.launch(headless=True)
            try:
                context=browser.new_context(viewport={'width':390 if mobile else 1150,'height':1000},accept_downloads=True)
                context.route('**/*',route_request)
                registry.sites = sites.choices()
                # The dashboard opens only at its launch address, which the page then drops from its address.
                page=context.new_page();page.goto(app.launch_url)
                expect(page.locator('#notice')).to_have_text('Library loaded.')
                assert page.url==site+'/',page.url
                expect(page.locator('#site')).to_have_value('tapas')
                expect(page.locator('#site option')).to_have_text(['Tapas','Webtoon — coming later','Tappytoon'])
                assert page.locator('#site option').evaluate_all('nodes => nodes.map(node => node.disabled)') == [False,True,False]
                navigation_controls(page,site)
                simple_account_controls(page,registry,alice,screenshot)
                mobile_data_controls(page,registry,alice)
                site_settings_controls(page,registry,alice)
                # This device only: cloud storage gets a card once it is set up.
                expect(page.locator('#library-locations .location-card')).to_have_count(1)
                open_b2(page)
                # Sharing one setup with other sites is offered only to a library the app linked, which this is not.
                expect(page.locator('#cloud-options input[type=checkbox]:visible')).to_have_count(0)
                expect(page.locator('#cloud-status')).to_contain_text('No connection configured')
                leave_b2(page)
                b2_setup_controls(page, registry)
                cloud_signin_controls(page, registry)
                cloud_controls(page, registry, alice)
                # Every request the pages made went to a route the dashboard still has.
                assert not [path for _,path,status in calls if path.startswith('/api/') and status in {404,405}]
                cookies=context.cookies()
                assert len(cookies)==1 and cookies[0]['httpOnly'] and cookies[0]['sameSite']=='Strict'
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert 'BOB_PRIVATE' not in page.locator('body').inner_text()
                assert not page.evaluate('Boolean(window.executed)')
                fetch='async path => (await fetch(path)).status'
                assert page.evaluate(fetch,'/api/v1/workspaces/'+bob.id)==404
                # Saved files open on this computer; nothing downloads them through the page.
                assert page.evaluate(fetch,'/api/v1/workspaces/'+alice.id+'/exports/'+'a'*64)==405
                held=threading.Event();release=threading.Event()
                def hold_import_lock():
                    with LibraryLock(alice.library):
                        held.set();release.wait(30)
                writer=threading.Thread(target=hold_import_lock);writer.start()
                try:
                    assert held.wait(5)
                    page.locator('#downloads').click()
                    # The one saved episode, listed once: its data files are under All files.
                    expect(page.locator('#files tr')).to_have_count(1)
                    original=next(alice.library.glob('*/*.cbz'))
                    row=page.locator('#files tr').filter(has_text='.cbz');button=row.locator('.file-action')
                    expect(button).to_have_text('Open')
                    with patch('comic_archive.local_web.subprocess.Popen') as launch:
                        launch.return_value.wait.return_value=0
                        button.click()
                        expect(row.locator('.file-action-status')).to_have_text('Sent to your device’s default app.')
                        assert launch.call_args.args[0]==['xdg-open',str(original)]
                finally:
                    release.set();writer.join(5)
                assert not writer.is_alive()
                if screenshot:
                    page.goto(site+'/')
                    expect(page.locator('#notice')).to_have_text('Library loaded.')
                    page.screenshot(path=str(screenshot),full_page=True)
                saved_library_controls(context, site, registry, alice, screenshot)
                own_window_controls(context, site, registry, alice)
                context.close()
            finally:browser.close()
        return {'browser':'firefox','offline':True,'source_requests':0,'owner_isolation':True,
                'mode':'local','local_signed_session':True,
                'title_html_not_executed':True,
                'no_removed_routes_requested':True,
                'downloads_route_removed':True,
                'simple_account_go':True,
                'account_list_live_counts_and_stall_feedback':True,
                'theme_device_default_toggle_reload_and_saved_files':True,
                'shared_navigation_both_views':True,
                'browser_only_site_signin':True,
                'b2_setup_signin_test_and_cleanup':True,
                'saved_file_table_and_pagination':True,
                'saved_file_action_buttons':True,
                'device_file_open_without_copy':True,
                'requests_checked':len(calls)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot',type=Path)
    parser.add_argument('--mobile',action='store_true',help='Check the narrow viewport layout')
    args=parser.parse_args()
    try:print(json.dumps(smoke(args.screenshot,args.mobile),indent=2))
    except Exception as error:
        print('Offline dashboard smoke test failed ('+type(error).__name__+').')
        # Keep failure locations useful without logging page contents or
        # unfiltered browser errors. Fixtures deliberately contain private fields.
        print(json.dumps({'frames': [{'function':frame.name, 'line':frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)
            if Path(frame.filename).name == 'dashboard_smoke.py']}))
        raise SystemExit(1) from None
