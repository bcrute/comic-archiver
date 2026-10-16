#!/usr/bin/env python3
"""Offline Firefox dashboard smoke test using synthetic owned workspaces.

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
from comic_archive.hosted import WorkspaceRegistry
from comic_archive.inventory import Inventory
from comic_archive.locking import LibraryLock
from comic_archive.local_web import LOCAL_OWNER, LocalApplication, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.web import HostedApplication
from comic_archive.worker import private_json


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(b'\0\x40\xa0\x80'))+chunk(b'IEND',b''))


def navigation_controls(page, site, simple):
    """Both views use identical controls; only their active page differs."""
    from playwright.sync_api import expect
    snapshot = """node => {const copy=node.cloneNode(true); copy.querySelectorAll('[aria-current]').forEach(link => link.removeAttribute('aria-current')); copy.querySelector('#site').removeAttribute('disabled'); return copy.outerHTML;}"""
    expect(page.locator('#save-nav')).to_have_text('Dashboard' if simple else 'Save episodes')
    expect(page.locator('.brand')).to_have_text('Comic Archiver')
    expect(page.locator('#save-nav')).to_have_attribute('href','/' if simple else '/#import-title')
    expect(page.locator('#save-nav')).to_have_attribute('aria-current','page')
    header = page.locator('header').evaluate(snapshot)
    other = page.context.new_page()
    try:
        other.goto(site+'/library')
        expect(other.locator('#notice')).to_have_text('Saved files loaded.')
        expect(other.locator('#library-nav')).to_have_attribute('aria-current','page')
        expect(other.locator('nav [aria-current=page]')).to_have_count(1)
        assert other.locator('header').evaluate(snapshot) == header
        expect(other.locator('#theme-toggle')).to_have_count(1)
        if simple: expect(other.locator('#advanced-nav')).to_be_hidden()
        else: expect(other.locator('#advanced-nav')).to_be_visible()
        other.locator('#save-nav').click()
        expect(other.locator('#save-nav')).to_have_attribute('aria-current','page')
        expect(other.locator('#account-download' if simple else '#download-options')).to_be_visible()
    finally:
        other.close()


def simple_account_controls(page, registry, workspace, local, screenshot):
    """The default surface has one account action; old controls stay in the DOM."""
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
    page.evaluate("() => {localStorage.removeItem('comic-archive-theme'); localStorage.setItem('tapas-archive-theme', 'dark');}")
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-theme','dark')
    assert page.evaluate("localStorage.getItem('comic-archive-theme')") == 'dark'
    for selector in ('#download-options','#advanced','#advanced-nav','#batch-progress','#mobile-options'):
        expect(page.locator(selector)).to_be_hidden()
    (expect(page.locator('#setup-b2')).to_be_visible() if local else expect(page.locator('#setup-b2')).to_be_hidden())
    expect(page.locator('#cloud-options')).to_be_hidden()
    expect(page.locator('#series-url')).to_have_count(1)
    # The one choice beside Go is the speed, and only the app on this computer offers it.
    assert page.locator('#account-download select').evaluate_all('nodes => nodes.map(node => node.id)') == ['account-speed']
    (expect(page.locator('#account-speed')).to_be_visible() if local else expect(page.locator('#account-speed')).to_be_hidden())
    # The mobile data choice appears only when the connection holds saving (mobile_data_controls); the B2 choice
    # sits beside the save folder, once a B2 connection has passed its check (b2_setup_controls).
    assert page.locator('#account-download input').evaluate_all('nodes => nodes.map(node => node.id)') == ['cloud-enabled', 'account-mobile-data']
    expect(page.locator('#account-network')).to_be_hidden()
    expect(page.locator('#cloud-choice')).to_be_hidden()
    if not local:
        expect(page.locator('#setup-b2')).to_be_hidden()
    if local:
        # A tall saved-library card must not bury the only cloud setup entry.
        expect(page.locator('#setup-b2')).to_be_in_viewport()
        page.locator('#library-locations').evaluate("node => node.style.minHeight = '1400px'")
        with patch.object(registry.cloud_signin, 'start') as connect:
            # B2 setup is a page of its own, opened from its top, with the dashboard set aside.
            page.locator('#setup-b2').click()
            expect(page.locator('#cloud-options')).to_be_visible()
            expect(page.locator('#cloud-title')).to_be_in_viewport()
            for selector in ('#account-download', '#library-overview', '#dashboard-heading'):
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
            page.locator('#cloud-back').click()
            expect(page.locator('#cloud-options')).to_be_hidden()
            expect(page.locator('#account-download')).to_be_visible()
            # The browser's own Back and Forward move between the two pages too.
            page.go_back()
            expect(page.locator('#cloud-options')).to_be_visible()
            page.go_forward()
            expect(page.locator('#account-download')).to_be_visible()
        page.locator('#library-locations').evaluate("node => node.style.minHeight = ''")
        page.evaluate('scrollTo(0,0)')
        expect(page.locator('#account-go')).to_be_enabled()
        expect(page.locator('#account-status')).to_contain_text('Ready. Hit Go')
        expect(page.locator('#account-save-title')).to_have_text('Ready to archive')
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
        expect(page.locator('#tapas-signin')).to_be_visible()
        # A downloader that is still starting is said to be starting, without offering to sign in again.
        private_json(worker_status,{'state':'starting'})
        page.evaluate('poll()')
        expect(page.locator('#dashboard-state')).to_have_text('Starting')
        expect(page.locator('#dashboard-worker')).to_have_text('Downloader · Starting')
        expect(page.locator('#account-status')).to_contain_text('Starting the downloader')
        expect(page.locator('#tapas-signin')).to_be_hidden()
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
        for estimate,text in (({'waiting_episodes':426,'unchecked_series':610,'seconds':None},'takes days or weeks'),
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
                           ({'between_steps_seconds':4},'archiver is connected')):
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
        expect(page.locator('#account-activity')).to_contain_text('archiver is connected')
        route_pattern='**/api/v1/workspaces/'+workspace.id+'/account'
        def unavailable(route):
            route.fulfill(status=503,content_type='application/json',body='{"code":"request_unavailable"}')
        page.route(route_pattern,unavailable)
        page.evaluate('poll()')
        expect(page.locator('#account-activity')).to_contain_text('current activity is unknown')
        expect(page.locator('#dashboard-state')).to_have_text('Reconnecting')
        expect(page.locator('#dashboard-worker')).to_have_text('Downloader · Status unknown')
        expect(page.locator('#account-list-count')).to_contain_text('4 pages read')
        page.unroute(route_pattern,unavailable)
        page.evaluate('poll()')
        expect(page.locator('#account-activity')).to_contain_text('archiver is connected')
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
        # A speed chosen while paused is kept, and Go continues the saved download at it.
        page.locator('#account-speed').select_option('slow')
        expect(page.locator('#account-speed-help')).to_contain_text('About one image a second')
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
        value.update(code='login_required')
        private_json(checkpoint,value)
        page.evaluate('poll()')
        expect(page.locator('#tapas-signin')).to_be_visible()
        expect(page.locator('#account-go')).to_be_disabled()
        signin=registry.tapas_signin
        with patch.object(signin,'start',return_value={'status':'running'}):
            signin._state={'status':'running'}
            page.locator('#tapas-signin').click()
            expect(page.locator('#tapas-signin-status')).to_contain_text('detect it automatically')
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
        expect(page.locator('#tapas-signin-status')).to_contain_text('retry automatically')
        page.unroute('**'+base,unavailable)
        page.evaluate('poll()')
        expect(page.locator('#tapas-signin-status')).to_have_text('')
    else:
        expect(page.locator('#account-go')).to_be_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if screenshot:
        page.screenshot(path=str(screenshot.with_name(screenshot.stem+'-simple.png')),full_page=True)


def saved_library_controls(context, site, registry, workspace, local, screenshot):
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
        # The library page fills its site list with its own copy of the code.
        if local: expect(page.locator('#site option')).to_have_text(['Tapas','Webtoon — coming later','Tappytoon'])
        else: expect(page.locator('#site-choice')).to_be_hidden()
        page.locator('#refresh-files').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–50 of 151 files.')
        expect(page.locator('#files tr')).to_have_count(50)
        expect(page.locator('#files a')).to_have_count(0)
        assert page.locator('#files .file-action').evaluate_all('nodes => nodes.every(n => n.tagName === "BUTTON" && n.textContent === "'+('Open' if local else 'Download')+'")')
        if local:
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
        expect(page.locator('#library-locations [data-location='+('all' if local else 'device')+']')).to_have_attribute('aria-pressed','true')
        page.locator('#page-size').select_option('100')
        expect(page.locator('#file-range')).to_have_text('Showing 1–100 of 151 files.')
        page.locator('#file-search').fill('NOT_SAVED_ANYWHERE');page.locator('#search-files').click()
        expect(page.locator('#file-range')).to_have_text('No saved files match these filters.')
        expect(page.locator('#files tr')).to_have_count(0)
        page.locator('#clear-search').click()
        expect(page.locator('#files tr')).to_have_count(100)
        page.locator('#file-sort').select_option('saved_asc')
        expect(page.locator('#notice')).to_have_text('Saved files loaded.')
        stamps = page.locator('#files time').evaluate_all('nodes => nodes.map(n => n.dateTime)')
        assert stamps == sorted(stamps)
        page.locator('#library-locations [data-location=device]').click()
        expect(page.locator('#file-range')).to_have_text('Showing 1–100 of 151 files.')
        if local:
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
            expect(page.locator('#file-range')).to_have_text('Showing 1–100 of 150 files.')
            # All, this device and B2: storage the app does not offer is not listed.
            expect(page.locator('#library-locations button')).to_have_count(3)
            expect(page.locator('#library-locations [data-location=proton]')).to_have_count(0)
            expect(page.locator('#library-summary')).to_contain_text('153 files and cloud copies')
            page.goto(site+'/library#'+workspace.id+'/b2'); page.reload()
            expect(page.locator('#files tr')).to_have_count(3)
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
        else:
            expect(page.locator('#library-locations button')).to_have_count(1)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if screenshot:
            page.screenshot(path=str(screenshot.with_name(screenshot.stem+'-library.png')), full_page=True)
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
    simple = registry.simple_mode
    registry.location, registry.theme, registry.simple_mode = Location(), Theme(), True
    page = context.new_page()
    try:
        for address in (site+'/', site+'/library#'+workspace.id+'/all'):
            page.goto(address)
            if address.endswith('/'):
                expect(page.locator('#save-location')).to_be_visible()
                expect(page.locator('#save-folder')).to_have_text('/home/user/Comic Archiver/Library')
                wait_for_ui_idle(page)
                # Cloud saving runs on the person's own computer, so the window offers it as the local browser version does.
                page.locator('#setup-b2').click()
                expect(page.locator('#cloud-options')).to_be_visible()
                # B2 was connected earlier in this run, so its setup steps are folded until asked for.
                expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(2)
                page.locator('#b2-steps-toggle').click()
                expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(6)
                expect(page.locator('#b2-login')).to_be_visible()
                # The person is signed in from creating the bucket, so the link opens the keys page itself.
                expect(page.locator('#cloud-options .cloud-steps a[href*="app_keys"]')).to_have_attribute(
                    'href', 'https://secure.backblaze.com/app_keys.htm')
                # The window's mobile data choice is beside Go, not a second one on this page.
                expect(page.locator('#allow-mobile-data')).to_be_hidden()
                page.locator('#cloud-back').click()
                # The window keeps where episodes go in one place: beside the save folder.
                assert page.evaluate("document.getElementById('save-location').nextElementSibling.id") == 'cloud-choice'
                with page.expect_response(lambda response: response.url.endswith('/api/v1/location')) as sent:
                    page.locator('#open-folder').click()
                assert sent.value.status == 200 and asked == ['open']
            else:
                expect(page.locator('#library-locations [data-location=all]')).to_be_visible()
                expect(page.locator('#library-locations [data-location=device]')).to_be_visible()
            wanted = 'light' if page.locator('html').get_attribute('data-theme') == 'dark' else 'dark'
            with page.expect_response(lambda response: response.url.endswith('/api/v1/theme')) as sent:
                page.locator('#theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme', wanted)
            assert sent.value.status == 200 and kept[-1] == wanted
        # Where the cloud helper is not offered (a device it has no pinned build for) neither page shows cloud saving.
        with patch('comic_archive.cloud_tools.platform.machine', return_value='riscv64'):
            page.goto(site+'/')
            expect(page.locator('#save-location')).to_be_visible()
            expect(page.locator('#cloud-entry')).to_be_hidden()
            page.goto(site+'/library#'+workspace.id+'/all')
            expect(page.locator('#library-locations [data-location=device]')).to_be_visible()
            expect(page.locator('#library-locations [data-location=all]')).to_have_count(0)
        # Choosing another site in either page's list leaves the window a request to open it. The window switches
        # behind the page, which waits, saying so, and then reads the other site without being reloaded.
        from comic_archive import app_runtime, sites
        example = replace(sites.DEFAULT, key='example', name='Example', available=True)
        listed, registry.site_choice = registry.sites, app_runtime.SiteChoice()
        with patch('comic_archive.sites.SITES', (sites.DEFAULT, example)):
            for address, said in ((site+'/', '#tapas-signin-status'), (site+'/library#'+workspace.id+'/device', '#notice')):
                registry.sites = sites.choices()
                page.goto(address)
                expect(page.locator('#site')).to_have_value('tapas')
                if address.endswith('/'):
                    wait_for_ui_idle(page)
                    expect(page.locator('#site')).to_be_enabled()
                page.evaluate('window.notReloaded = true')
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
                    expect(page.locator('#tapas-signin')).to_have_text('Sign in to Example')
        registry.sites = listed
    finally:
        page.close()
        registry.location = registry.theme = registry.site_choice = None; registry.simple_mode = simple


def mobile_data_controls(page, registry, workspace):
    """Beside Go, the mobile data choice appears when the connection holds saving, and applies when ticked."""
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
    expect(page.locator('#account-network')).to_be_hidden()
    assert 'mobile data' not in held_message()
    for probe, reason in (({'connection':'cellular', 'metered':True}, 'This connection is metered'),
                          ({'connection':'unknown', 'metered':None}, 'cannot tell')):
        with patch('comic_archive.network.probe', return_value=probe):
            page.evaluate('network(false)')
            expect(page.locator('#account-network')).to_be_visible()
            expect(page.locator('#account-mobile-data')).not_to_be_checked()
            expect(page.locator('#account-network-help')).to_contain_text(reason)
            assert 'allow mobile data, then hit Go' in held_message()
    with patch('comic_archive.network.probe', return_value={'connection':'cellular', 'metered':True}):
        page.evaluate('network(false)')
        with page.expect_response(lambda response: response.url.endswith('/network') and response.request.method == 'POST'):
            page.locator('#account-mobile-data').check()
        expect(page.locator('#account-network-help')).to_contain_text('any connection')
        assert wifi_only() is False
        # Still shown once allowed, so it can be switched off again.
        expect(page.locator('#account-network')).to_be_visible()
        assert 'mobile data' not in held_message()
        with page.expect_response(lambda response: response.url.endswith('/network') and response.request.method == 'POST'):
            page.locator('#account-mobile-data').uncheck()
        assert wifi_only() is True
        expect(page.locator('#account-network-help')).to_contain_text('This connection is metered')
    page.evaluate('network(false)')
    expect(page.locator('#account-network')).to_be_hidden()


def b2_setup_controls(page, registry):
    """Four-step default setup, private key clearing, nearby feedback and reload."""
    from playwright.sync_api import expect
    page.reload(); open_b2(page)
    expect(page.locator('#cloud-provider')).to_have_value('b2')
    expect(page.locator('#cloud-provider')).to_be_hidden()
    expect(page.locator('#cloud-options .cloud-steps > li:visible')).to_have_count(6)
    # The top of the page says whether B2 is set up.
    expect(page.locator('#b2-state-pill')).to_have_text('Not set up')
    expect(page.locator('#b2-steps-fold')).to_be_hidden()
    expect(page.locator('#connect-cloud')).to_be_hidden()
    expect(page.locator('#b2-bucket-name')).to_have_value(re.compile(r'comic-library-[a-f0-9]{12}'))
    expect(page.locator('#b2-key-bucket')).to_have_text(page.locator('#b2-bucket-name').input_value())
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
        # Leaving the page lets them go.
        page.locator('#cloud-back').click(); open_b2(page)
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
        expect(page.locator('#setup-b2')).to_have_text('Open B2 storage')
        # Where episodes go is chosen beside the save folder, once the check has passed; the page leads there.
        page.locator('#cloud-done').click()
        expect(page.locator('#cloud-options')).to_be_hidden()
        expect(page.locator('#cloud-choice')).to_be_hidden()
        tested.append(True); page.evaluate('cloud()')
        open_b2(page)
        expect(state()[0]).to_have_text('Set up')
        expect(state()[1]).to_contain_text('Uploads are off; turn them on from the dashboard')
        page.locator('#cloud-back').click()
        expect(page.locator('#cloud-choice')).to_be_visible()
        expect(page.locator('#cloud-choice')).to_contain_text('Upload new episodes to Backblaze B2, bucket My-Comics-2026,')
        # In this fuller view, it is in the saving step, after where episodes are saved now.
        assert page.evaluate("document.getElementById('save-destination').nextElementSibling.id") == 'cloud-choice'
        # A refused choice says why and shows what is saved.
        with patch.object(registry, 'configure_cloud', side_effect=ArchiveError('Fixture refusal.')):
            page.locator('#cloud-enabled').click()
            expect(page.locator('#cloud-choice-status')).to_have_text(re.compile(r'.+'))
            expect(page.locator('#cloud-enabled')).not_to_be_checked()
        with patch.object(registry, 'configure_cloud', return_value={}) as configure:
            page.locator('#cloud-enabled').click()
            expect(page.locator('#cloud-choice-status')).to_have_text('')
            assert configure.call_args.args[2:] == ('b2', True)
        enabled.append(True); open_b2(page); page.evaluate('cloud()')
        expect(state()[0]).to_have_text('Uploading to B2')
        failed.append(1); page.evaluate('cloud()')
        expect(state()[0]).to_have_text('Uploads need attention')
        expect(state()[1]).to_contain_text('1 upload needs retrying: click Retry unfinished uploads below.')
    page.reload(); open_b2(page)


def open_b2(page):
    """B2 setup is a page of its own, at #b2, in place of the dashboard."""
    from playwright.sync_api import expect
    page.evaluate("location.hash = 'b2'")
    expect(page.locator('#cloud-options')).to_be_visible()
    expect(page.locator('#account-download')).to_be_hidden()


def leave_b2(page):
    """A fresh dashboard, as the app starts."""
    page.goto(page.url.split('#')[0])


def select_hidden_provider(page, provider):
    """Retain regression coverage for providers hidden during B2 acceptance."""
    page.evaluate("provider => {const select=document.getElementById('cloud-provider'); select.value=provider; select.dispatchEvent(new Event('change'));}", provider)


def cloud_signin_controls(page, registry):
    """Exercise button sign-in and local credential clearing without provider traffic."""
    from playwright.sync_api import expect
    signin = registry.cloud_signin
    started = []
    connected = False
    original = registry.cloud_status
    def status(owner, workspace):
        value = original(owner, workspace)
        for target in value['targets']:
            if target['provider'] == 'google': target['connected'] = connected
        return value
    def start(provider, credentials):
        started.append((provider, dict(credentials)))
        signin._state = {'status':'running','provider':provider}
        if provider in ('google', 'dropbox'): signin._state['auth_url'] = 'http://127.0.0.1:53682/auth?state='+'a'*32
        return signin.status()
    def cancel():
        signin._state = {'status':'cancelled'}
        return signin.status()
    with patch.object(signin, 'start', side_effect=start), patch.object(signin, 'cancel', side_effect=cancel), patch.object(registry, 'cloud_status', side_effect=status):
        select_hidden_provider(page, 'google')
        page.locator('#connect-cloud').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Complete Google Drive sign-in')
        expect(page.locator('#cloud-auth-link')).to_be_visible()
        expect(page.locator('#connect-cloud')).to_be_disabled()
        expect(page.locator('#cloud-provider')).to_be_disabled()
        page.reload();open_b2(page)
        expect(page.locator('#cloud-auth-status')).to_contain_text('Complete Google Drive sign-in')
        page.locator('#cancel-cloud-signin').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('cancelled')
        expect(page.locator('#connect-cloud')).to_be_enabled()
        page.locator('#connect-cloud').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Complete Google Drive sign-in')
        connected = True;signin._state = {'status':'connected','provider':'google'}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('Signed in')
        expect(page.locator('#test-cloud')).to_be_enabled()
        wait_for_ui_idle(page)
        signin._state = {'status':'connected','provider':'google','reused_session':True}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('don’t need to sign in again')
        for code, message in [('cloud_rate_limited', 'signing in again won’t fix'),
                              ('cloud_client_required', 'update from the app maintainer')]:
            wait_for_ui_idle(page)
            signin._state = {'status':'error','provider':'google','code':code}
            page.evaluate('poll()')
            expect(page.locator('#cloud-auth-status')).to_contain_text(message)
        wait_for_ui_idle(page)
        select_hidden_provider(page, 'dropbox')
        expect(page.locator('#connect-cloud')).to_have_text('Sign in to Dropbox')
        page.locator('#connect-cloud').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Complete Dropbox sign-in')
        expect(page.locator('#cloud-auth-link')).to_be_visible()
        assert started[-1] == ('dropbox', {})
        page.locator('#cancel-cloud-signin').click()
        expect(page.locator('#connect-cloud')).to_be_enabled()
        select_hidden_provider(page, 'proton')
        page.locator('#connect-cloud').click()
        expect(page.locator('#proton-signin')).to_be_visible()
        page.locator('#cloud-username').fill('user@example.test')
        page.locator('#cloud-password').fill('PRIVATE_PROVIDER_PASSWORD')
        page.locator('#cloud-code').fill('123456')
        page.locator('#submit-cloud-login').click()
        expect(page.locator('#cloud-auth-status')).to_contain_text('Signing in to Proton')
        expect(page.locator('#proton-signin')).to_be_hidden()
        expect(page.locator('#cloud-password')).to_have_value('')
        expect(page.locator('#cloud-code')).to_have_value('')
        assert started[-1] == ('proton', {'username':'user@example.test','password':'PRIVATE_PROVIDER_PASSWORD','code':'123456','mailbox_password':''})
        assert 'PRIVATE_PROVIDER_PASSWORD' not in page.content()
        page.reload();open_b2(page)
        expect(page.locator('#cloud-provider')).to_have_value('proton')
        expect(page.locator('#cloud-auth-status')).to_contain_text('Signing in to Proton')
        expect(page.locator('#cloud-provider')).to_be_disabled()
        expect(page.locator('#cloud-password')).to_have_value('')
        signin._state = {'status':'error','provider':'proton','code':'signin_failed'}
        page.evaluate('poll()')
        expect(page.locator('#cloud-auth-status')).to_contain_text('try again')
        select_hidden_provider(page, 'google')
    signin._state = {'status':'idle'}
    leave_b2(page)
    page.locator('#advanced > summary').click()
    page.locator('#series-options > summary').click()


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
            if target['provider'] == 'google':
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
        select_hidden_provider(page, 'google')
        expect(page.locator('#test-cloud')).to_be_enabled()
        page.locator('#test-cloud').click()
        try:expect(page.locator('#notice')).to_contain_text('Testing two completed episodes')
        except AssertionError:
            # Fixed UI error labels and booleans only; never log fixture data.
            print(json.dumps(page.evaluate('''({simple:simpleMode(),busy:state.busy,
                taskKind:state.taskKind,noticeHidden:document.getElementById('notice').hidden,
                errorCode:Object.entries(errors).find(([key,value]) => value === document.getElementById('notice').textContent)?.[0] || 'unknown'})''')))
            raise
        expect(page.locator('#test-cloud')).to_be_disabled()
        expect(page.locator('#cloud-action-status')).to_contain_text('Testing two completed episodes')
        if registry.simple_mode:
            expect(page.locator('#account-go')).to_be_disabled()
        assert complete('cloud_test', {'verified_episodes':2,'test_files_deleted':True,'local_archives_retained':True}) == {'provider':'google'}
        tested = True
        test_id = 'f257c685-2a39-4e16-8dc0-353338e2f7d7'
        receipt = workspace.library/'.archive-state/cloud/tests'/f'{test_id}.json'
        receipt.parent.mkdir(parents=True, exist_ok=True)
        private_json(receipt, {'provider':'google','state':'cleanup_pending','error_code':'cloud_login_required'})
        expect(page.locator('#notice')).to_contain_text('2 episodes verified', timeout=10000)
        expect(page.locator('#test-cloud')).to_be_disabled()
        cleanup = page.locator('#cloud-cleanup button')
        expect(cleanup).to_have_text('Retry Google Drive test cleanup')
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
        private_json(receipt, {'provider':'google','state':'cleaned'})
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
    expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')


def complete_review(page, workspace, *, planned=1, fits=True):
    """Finish only the synthetic metadata preview; never submit an import."""
    tasks=[]
    for path in (workspace.session/'worker/requests').glob('*.json'):
        request=json.loads(path.read_text())
        if request['operation']=='preview_series':
            tasks.append((path,request))
    assert len(tasks)==1
    path,request=tasks[0]
    private_json(workspace.session/'worker/responses'/path.name,{'task_id':path.stem,'status':'ok',
        'result':{'storage_preview':{'accessible_episode_count':2,'already_saved_episode_count':1,
            'planned_episode_count':planned,'estimated_additional_archive_bytes':20_000_000,
            'estimated_required_free_bytes':1_100_000_000,'free_bytes':2_000_000_000,
            'fits_estimate':fits,'storage_destination':'device','content_type':'comic',
            'content_type_verified':True}}})
    path.unlink();page.evaluate('poll()')
    return request


def first_download_controls(page, registry, workspace):
    """A user can review visible choices without finding Advanced or queue actions."""
    from playwright.sync_api import expect
    expect(page.locator('#advanced')).not_to_have_attribute('open','')
    expect(page.locator('#local-setup')).to_be_hidden()
    expect(page.locator('#library-picker')).to_be_hidden()
    expect(page.locator('#next-step')).to_contain_text('Paste a series link')
    expect(page.locator('#preview-series')).to_be_disabled()
    expect(page.locator('#import-link')).to_be_disabled()
    assert page.locator('#library-title').bounding_box()['y'] < page.locator('#import-title').bounding_box()['y']
    assert page.locator('#series-url').bounding_box()['y'] < page.locator('#overview-title').bounding_box()['y']
    page.locator('#series-url').fill('https://tapas.io/series/first/info')
    expect(page.locator('#next-step')).to_contain_text('click Review download')
    expect(page.locator('#preview-series')).to_be_enabled()
    expect(page.locator('#import-link')).to_be_disabled()
    page.locator('#image-profile').select_option('small')
    # The fuller view's mobile data choice is with its other saving choices, not inside the B2 page.
    expect(page.locator('#mobile-options')).to_be_visible()
    # The fuller view's way to B2 is beside its local saving step.
    expect(page.locator('#open-b2')).to_be_visible()
    expect(page.locator('#cloud-options')).to_be_hidden()
    # Enter also reviews rather than bypassing the first check.
    wait_for_ui_idle(page)
    expect(page.locator('#preview-series')).to_be_enabled()
    page.locator('#series-url').press('Enter')
    try:
        expect(page.locator('#notice')).to_contain_text('No import was started', timeout=30000)
    except AssertionError:
        print(json.dumps(page.evaluate("""({firstReviewFailure:true,busy:state.busy,
            polling:state.polling,taskKind:state.taskKind,
            worker:state.status?.worker?.reported_state,
            selectionChanged:document.getElementById('notice').textContent.includes('choices changed'),
            errorCode:Object.entries(errors).find(([key,value]) => value === document.getElementById('notice').textContent)?.[0] || 'unknown'})""")))
        raise
    expect(page.locator('#next-step')).to_contain_text('No episodes are being downloaded')
    assert registry.storage(LOCAL_OWNER,workspace.id)['image_profile']=='small'
    assert registry.network_status(LOCAL_OWNER,workspace.id)['wifi_only'] is True
    assert complete_review(page,workspace)=={'operation':'preview_series','args':{
        'url':'https://tapas.io/series/first/info','scope':'one'}}
    expect(page.locator('#import-link')).to_be_enabled()
    expect(page.locator('#import-link')).to_have_text('Download one episode')
    expect(page.locator('#next-step')).to_contain_text('Click Download')
    with patch('comic_archive.network.probe', return_value={'connection':'cellular', 'metered':True}):
        page.evaluate('network()')
        expect(page.locator('#next-step')).to_contain_text('Mobile data is off')
        expect(page.locator('#allow-mobile-data')).not_to_be_checked()
    page.evaluate('network()')
    expect(page.locator('#next-step')).to_contain_text('Click Download')
    assert not list((workspace.session/'worker/requests').glob('*.json'))
    page.locator('#series-amount').select_option('batch')
    expect(page.locator('#import-link')).to_be_disabled()
    expect(page.locator('#next-step')).to_contain_text('click Review download')
    page.locator('#image-profile').select_option('original')
    page.locator('#preview-series').click()
    expect(page.locator('#notice')).to_contain_text('No import was started')
    complete_review(page,workspace,planned=0)
    expect(page.locator('#next-step')).to_contain_text('No new accessible episodes')
    expect(page.locator('#import-link')).to_be_disabled()
    page.locator('#preview-series').click()
    expect(page.locator('#notice')).to_contain_text('No import was started')
    complete_review(page,workspace,fits=False)
    expect(page.locator('#series-storage')).to_contain_text('free space on this computer')
    assert 'cloud' not in page.locator('#series-storage').inner_text().lower()
    expect(page.locator('#next-step')).to_contain_text('Space is limited')
    expect(page.locator('#advanced')).not_to_have_attribute('open','')
    page.locator('#series-url').fill('');page.locator('#series-amount').select_option('one')


def storage_preview_controls(page, workspace):
    from playwright.sync_api import expect
    url='https://tapas.io/series/sample/info'
    wait_for_ui_idle(page)
    page.locator('#series-url').fill(url)
    page.locator('#series-amount').select_option('batch')
    page.locator('#preview-series').click()
    try:
        expect(page.locator('#notice')).to_contain_text('No import was started', timeout=10000)
    except AssertionError:
        print(json.dumps(page.evaluate("""({previewFailure:true,busy:state.busy,
            taskKind:state.taskKind,worker:state.status?.worker?.reported_state,
            progressStatus:state.status?.progress?.status,
            errorCode:Object.entries(errors).find(([key,value]) => value === document.getElementById('notice').textContent)?.[0] || 'unknown'})""")))
        raise
    requests=workspace.session/'worker/requests';files=list(requests.glob('*.json'))
    assert len(files)==1
    task=files[0]
    assert json.loads(task.read_text())=={'operation':'preview_series','args':{'url':url,'scope':'series'}}
    preview={'accessible_episode_count':102,'already_saved_episode_count':2,'planned_episode_count':100,
        'estimated_additional_archive_bytes':20_000_000_000,'free_bytes':2_000_000_000,
        'estimated_required_free_bytes':25_000_000_000,'fits_estimate':False,'storage_destination':'device',
        'content_type':'comic','content_type_verified':True,'cookie':'PRIVATE_COOKIE','title':'PRIVATE_TITLE'}
    def complete():
        private_json(workspace.session/'worker/responses'/task.name,{'task_id':task.stem,'status':'ok',
            'result':{'storage_preview':preview,'account_id':'PRIVATE'}})
        task.unlink();page.evaluate('poll()')
    complete()
    expect(page.locator('#series-storage')).to_contain_text('100 new episodes')
    expect(page.locator('#series-storage')).to_contain_text('Space is limited')
    expect(page.locator('#series-storage')).to_contain_text('cloud storage, smaller images')
    assert 'PRIVATE' not in page.locator('body').inner_text()
    assert not list(requests.glob('*.json')), 'A preview must not queue an import'
    expect(page.locator('#import-link')).to_be_enabled()
    page.locator('#preview-series').click()
    expect(page.locator('#notice')).to_contain_text('No import was started')
    task=next(requests.glob('*.json'))
    page.locator('#series-url').fill('https://tapas.io/series/different/info')
    complete()
    expect(page.locator('#series-storage')).to_contain_text('previous selection')
    expect(page.locator('#import-link')).to_be_disabled()
    page.locator('#series-url').fill('');page.locator('#series-amount').select_option('one')


def connection_check_controls(page, requests, responses, existing_task):
    from playwright.sync_api import expect
    button=page.locator('#check');feedback=page.locator('#connection-check-status')
    button.click()
    expect(feedback).to_contain_text('Connection check queued')
    expect(feedback).to_be_in_viewport()
    expect(feedback).to_have_attribute('role','status')
    expect(button).to_have_attribute('aria-describedby','connection-check-status')
    expect(button).to_have_attribute('aria-busy','true')
    expect(button).to_have_text('Checking Tapas connection…')
    expect(button).to_be_disabled()
    button_box=button.bounding_box();feedback_box=feedback.bounding_box()
    assert 0 <= feedback_box['y']-(button_box['y']+button_box['height']) <= 16
    health=next(path for path in requests.glob('*.json') if path.stem!=existing_task)
    assert json.loads(health.read_text())=={'operation':'check_session','args':{}}
    # A transient status fetch failure is visible at the button; polling keeps
    # the original queued task, rather than sending another source check.
    route_pattern='**/tasks/'+health.stem
    page.route(route_pattern,lambda route:route.abort())
    page.evaluate('poll()')
    expect(feedback).to_contain_text('Connection lost')
    expect(feedback).to_contain_text('Waiting to retry')
    expect(button).to_be_disabled()
    page.unroute(route_pattern)
    assert len(list(requests.glob('*.json')))==2
    private_json(responses/health.name,{'task_id':health.stem,'status':'ok',
        'result':{'authenticated':True,'account_binding':'matches'}})
    health.unlink();page.evaluate('poll()')
    expect(feedback).to_contain_text('authentication verified and account matches')
    expect(button).to_have_attribute('aria-busy','false')
    expect(button).to_have_text('Check Tapas connection')
    expect(button).to_be_enabled()
    page.evaluate('refresh()')
    expect(feedback).to_contain_text('authentication verified and account matches')
    for result,message in [
        ({'status':'error','error_code':'login_required','error':'PRIVATE_RESPONSE_BODY'},'fresh login'),
        ({'status':'ok','result':{'authenticated':False}},'could not be verified'),
        ({'status':'ok','result':{'authenticated':True,'account_binding':'unbound'}},'bound on its first import')]:
        button.click()
        expect(feedback).to_contain_text('Connection check queued')
        expect(feedback).not_to_have_class('error')
        current=next(path for path in requests.glob('*.json') if path.stem!=existing_task)
        private_json(responses/current.name,{'task_id':current.stem,**result})
        current.unlink();page.evaluate('poll()')
        expect(feedback).to_contain_text(message)
        expect(button).to_be_enabled()
        if result.get('result',{}).get('authenticated') is not True:
            expect(feedback).to_have_class('error')
        assert 'PRIVATE_RESPONSE_BODY' not in page.locator('body').inner_text()
    # Queue submission itself can fail before a task exists.
    route_pattern='**/session/check'
    page.route(route_pattern,lambda route:route.fulfill(status=503,
        content_type='application/json',body=json.dumps({'code':'service_unavailable'})))
    button.click()
    expect(feedback).to_contain_text('temporarily unavailable')
    expect(feedback).to_have_class('error')
    expect(button).to_have_attribute('aria-busy','false')
    expect(button).to_be_enabled()
    page.unroute(route_pattern)
    assert len(list(requests.glob('*.json')))==1
    return health


def smoke(screenshot=None,mobile=False,local=False):
    from playwright.sync_api import sync_playwright,expect
    # Shared builders can delay durable fixture writes; retain bounded assertions.
    expect.set_options(timeout=10000)
    with tempfile.TemporaryDirectory(prefix='comic-dashboard-test-') as work, \
            patch('comic_archive.network.probe', return_value={'connection':'wired', 'metered':False}):
        if local:
            project=Path(work)/'project';project.mkdir()
            registry=LocalWorkspaceRegistry(project)
            alice=registry.resolve(LOCAL_OWNER,registry.workspace_id)
            bob=WorkspaceRegistry(Path(work)/'other-owner').create('synthetic-bob')
        else:
            registry=WorkspaceRegistry(Path(work)/'hosted')
            alice=registry.create('synthetic-alice');bob=registry.create('synthetic-bob')
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
        requests=alice.session/'worker/requests';requests.mkdir(parents=True)
        responses=alice.session/'worker/responses';responses.mkdir()
        private_json(alice.session/'worker/status.json',{'state':'ready'})
        site='http://127.0.0.1:8765' if local else 'https://archive.example'
        app=LocalApplication(registry,8765) if local else HostedApplication(registry,lambda env:'synthetic-alice',site)
        calls=[]

        def route_request(route):
            request=route.request;url=urlsplit(request.url)
            if url.scheme+'://'+url.netloc!=site:
                raise AssertionError('Unexpected external browser request')
            calls.append((request.method,url.path))
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
            route.fulfill(status=int(status.split()[0]),headers=values,body=payload)

        with sync_playwright() as playwright:
            browser=playwright.firefox.launch(headless=True)
            try:
                context=browser.new_context(viewport={'width':390 if mobile else 1150,'height':1000},accept_downloads=True)
                context.route('**/*',route_request)
                if local: registry.sites = sites.choices()
                page=context.new_page();page.goto(site+'/')
                expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')
                if local:
                    expect(page.locator('#site')).to_have_value('tapas')
                    expect(page.locator('#site option')).to_have_text(['Tapas','Webtoon — coming later','Tappytoon'])
                    assert page.locator('#site option').evaluate_all('nodes => nodes.map(node => node.disabled)') == [False,True,False]
                else:
                    expect(page.locator('#site-choice')).to_be_hidden()
                navigation_controls(page,site,True)
                simple_account_controls(page,registry,alice,local,screenshot)
                if local:
                    mobile_data_controls(page,registry,alice)
                    cloud_controls(page,registry,alice)
                assert not any(path.endswith('/series') or path.endswith('/storage') for _,path in calls)
                if not local:
                    assert not any('/cloud' in path or path.endswith('/network') for _,path in calls)
                registry.simple_mode = False
                page.reload()
                expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')
                navigation_controls(page,site,False)
                expect(page.locator('#storage-space')).to_contain_text('free on this device' if local else 'free on the library server')
                expect(page.locator('#storage-per-episode')).to_contain_text('per episode')
                expect(page.locator('#image-profile')).to_have_value('original')
                expect(page.locator('#cloud-choice')).to_be_hidden()
                expect(page.locator('#retry-cloud')).to_be_disabled()
                expect(page.locator('#test-cloud')).to_be_disabled()
                if local:
                    first_download_controls(page,registry,alice)
                (expect(page.locator('#mobile-options')).to_be_visible() if local else expect(page.locator('#mobile-options')).to_be_hidden())
                (expect(page.locator('#open-b2')).to_be_visible() if local else expect(page.locator('#open-b2')).to_be_hidden())
                # This device and B2 only: storage the app does not offer gets no card.
                expect(page.locator('#library-locations .location-card')).to_have_count(2 if local else 1)
                if not local:
                    assert 'Cloud episode copies' not in page.locator('#metrics').inner_text()
                    assert not any('/cloud' in path or path.endswith('/network') for _,path in calls)
                registry.desktop_only = False
                page.reload()
                expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')
                # The first download above never needed these secondary controls.
                page.locator('#advanced > summary').click()
                page.locator('#series-options > summary').click()
                # The mobile data choice is with the other saving choices, not on the B2 page.
                if local:
                    page.locator('#allow-mobile-data').check()
                    page.locator('#save-network').click()
                    expect(page.locator('#network-status')).to_have_text('All connections allowed, including mobile data.')
                    page.locator('#allow-mobile-data').uncheck()
                    page.locator('#save-network').click()
                    expect(page.locator('#allow-mobile-data')).not_to_be_checked()
                else:
                    expect(page.locator('#save-network')).to_be_disabled()
                    expect(page.locator('#allow-mobile-data')).to_be_disabled()
                    expect(page.locator('#network-status')).to_contain_text('local archiver')
                page.locator('#open-b2').click()
                expect(page.locator('#cloud-options')).to_be_visible()
                expect(page.locator('#cloud-options input[type=checkbox]')).to_have_count(0)
                if local:
                    expect(page.locator('#cloud-status')).to_contain_text('No connection configured')
                    select_hidden_provider(page, 'proton')
                    expect(page.locator('#cloud-setup')).to_contain_text('not officially supported by Proton')
                    select_hidden_provider(page, 'google')
                    expect(page.locator('#cloud-setup')).to_contain_text('Click Sign in')
                    expect(page.locator('#connect-cloud')).to_have_text('Sign in to Google Drive')
                    assert 'cloud-connect' not in page.locator('#cloud-options').inner_text()
                else:
                    expect(page.locator('#cloud-status')).to_contain_text('hosted dashboard cannot connect your device')
                if local:
                    b2_setup_controls(page, registry)
                    cloud_signin_controls(page, registry)
                    cloud_controls(page, registry, alice)
                    storage_preview_controls(page, alice)
                    page.locator('#advanced > summary').click()
                    page.locator('#series-options > summary').click()
                else:
                    expect(page.locator('#preview-series')).to_be_disabled()
                registry.desktop_only = True
                page.reload()
                expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')
                page.locator('#advanced > summary').click()
                page.locator('#series-options > summary').click()
                (expect(page.locator('#open-b2')).to_be_visible() if local else expect(page.locator('#open-b2')).to_be_hidden())
                (expect(page.locator('#network-options')).to_be_visible() if local else expect(page.locator('#network-options')).to_be_hidden())
                page.locator('#image-profile').select_option('small')
                page.locator('#save-storage').click()
                expect(page.locator('#notice')).to_contain_text('Quality choice saved')
                page.locator('#image-profile').select_option('original')
                page.locator('#save-storage').click()
                expect(page.locator('#notice')).to_contain_text('Quality choice saved')
                expect(page.locator('#scan-freshness')).to_contain_text('1 of 2 series successfully checked in the last 24 hours.')
                expect(page.locator('#scan-freshness')).to_contain_text('1 have no recorded complete check.')
                if local:
                    cookies=context.cookies()
                    assert len(cookies)==1 and cookies[0]['httpOnly'] and cookies[0]['sameSite']=='Strict'
                    expect(page.locator('#create')).to_be_disabled()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert 'BOB_PRIVATE' not in page.locator('body').inner_text()
                assert page.locator('#series img').count()==0
                assert not page.evaluate('Boolean(window.executed)')
                expect(page.locator('#series tr')).to_have_count(2)
                page.locator('#unlocked-only').check()
                expect(page.locator('#series tr')).to_have_count(1)
                expect(page.locator('#series tr td').nth(2)).to_have_text('1')
                page.locator('#search').fill('Another')
                expect(page.locator('#series tr')).to_have_count(0)
                page.locator('#unlocked-only').uncheck()
                expect(page.locator('#series tr')).to_have_count(1)
                page.locator('#search').fill('<img')
                expect(page.locator('#series tr')).to_have_count(1)
                page.locator('#series button').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                files=list(requests.glob('*.json'));assert len(files)==1
                task=files[0].stem;request=json.loads(files[0].read_text())
                assert request['operation']=='archive_watched_series' and request['args']['id']=='42'
                expect(page.locator('#speed')).to_have_value('fast' if local else 'normal')
                assert request['args']['speed']==('fast' if local else 'normal')
                assert request['args']['max_bytes']==(8*1024**3 if local else 256*1024**2)
                assert request['args']['max_seconds']==900 and request['args']['max_pages']==1000
                assert not list(bob.session.glob('worker/requests/*.json'))
                private_json(responses/(task+'.json'),{'task_id':task,'status':'ok','result':{
                    'status':'finished','archived_episode_ids':[],'enriched_series_ids':['42'],
                    'downloaded_pages':0,'downloaded_bytes':0,'downloaded_covers':0}})
                page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('Series details updated. Saved episodes were reused.')
                page.locator('#series button').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                retry=next(path for path in requests.glob('*.json') if path.stem!=task)
                private_json(responses/retry.name,{'task_id':retry.stem,'status':'ok','result':{
                    'status':'finished','archived_episode_ids':[],'metadata_unavailable_series_count':1,
                    'elapsed_seconds':95,'scan_elapsed_seconds':30,
                    'metadata_unavailable_series':[{'code':'PRIVATE_METADATA_ERROR'}]}})
                retry.unlink();page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('series details need a retry')
                expect(page.locator('#batch-timing')).to_contain_text('1m 35s total; 0m 30s checking lists and series')
                assert 'PRIVATE' not in page.locator('body').inner_text()
                # Stop a second import while queued. The fixture worker retains
                # the request and publishes the same fixed zero-request receipt.
                page.locator('#watch').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                expect(page.locator('#account-progress')).to_contain_text('Known series check queued')
                expect(page.locator('#watch')).to_have_text('Check queued…')
                expect(page.locator('#watch')).to_be_disabled()
                expect(page.locator('#account-progress')).to_have_attribute('aria-busy', 'true')
                queued=next(path for path in requests.glob('*.json') if path.stem!=task)
                page.evaluate('poll()')
                expect(page.locator('#stop-queued')).to_be_enabled()
                page.locator('#stop-queued').click()
                expect(page.locator('#notice')).to_contain_text('Queued stop requested')
                expect(page.locator('#stop-queued')).to_be_disabled()
                marker=alice.session/'worker/cancellations'/queued.name
                assert json.loads(marker.read_text())=={'document_type':'queued_comic_import_stop','task_id':queued.stem}
                assert queued.is_file() and not list(bob.session.glob('worker/cancellations/*'))
                from comic_archive.task_cancellation import cancelled_result
                private_json(responses/queued.name,{'task_id':queued.stem,'status':'ok',
                    'result':cancelled_result(alice.library,'sync')})
                queued.unlink()
                page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('stopped before it started')
                expect(page.locator('#account-progress')).to_contain_text('stopped before it started')
                expect(page.locator('#watch')).to_have_text('Check known series')
                page.locator('#watch').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                completed=next(path for path in requests.glob('*.json') if path.stem!=task)
                private_json(responses/completed.name,{'task_id':completed.stem,'status':'ok','result':{
                    'status':'finished','archived_episode_ids':[], 'refresh':{
                        'series_checks_completed':2,'series_check_target_count':5,
                        'successful_series_checks':2,'held_series_checks':0,'remaining_series_count':3}}})
                completed.unlink();page.evaluate('poll()')
                expect(page.locator('#account-progress')).to_contain_text('2 of 5 checked')
                expect(page.locator('#account-progress')).to_contain_text('3 remaining in this scan cycle')
                expect(page.locator('#account-progress')).to_contain_text('0 episodes saved')
                page.locator('#watch').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                paused=next(path for path in requests.glob('*.json') if path.stem!=task)
                private_json(responses/paused.name,{'task_id':paused.stem,'status':'ok','result':{
                    'status':'paused','pause_reason':'network_unavailable','source_http_status':503,
                    'archived_episode_ids':[],'downloaded_pages':1,'downloaded_bytes':1}})
                paused.unlink()
                page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('Tapas is temporarily unavailable')
                assert 'Import paused' in page.locator('#notice').inner_text()
                health=connection_check_controls(page,requests,responses,task)
                page.locator('#enabled').check()
                page.locator('#save-schedule').click()
                expect(page.locator('#notice')).to_contain_text('Schedule update queued')
                schedule=next(path for path in requests.glob('*.json') if path.stem not in {task,health.stem})
                request=json.loads(schedule.read_text());assert request['operation']=='configure_daily'
                assert request['args']['options']['free_only'] is True and request['args']['options']['max_seconds']==900
                from comic_archive.daily import configure
                args=request['args'].copy();args['zone']=args.pop('timezone')
                configure(alice.session,**args)
                private_json(responses/schedule.name,{'task_id':schedule.stem,'status':'ok','result':{}})
                page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('Daily schedule updated')
                expect(page.locator('#daily-status')).to_contain_text('Daily checks enabled')
                page.locator('#series-url').fill('https://tapas.io/series/chosen/info')
                page.locator('#speed').select_option('fast')
                if not local:
                    page.locator('#image-profile').select_option('small')
                if local:
                    expect(page.locator('#import-link')).to_be_disabled()
                    page.locator('#preview-series').click()
                    expect(page.locator('#notice')).to_contain_text('No import was started')
                    complete_review(page,alice)
                page.locator('#import-link').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                selected_link=next(path for path in requests.glob('*.json') if path.stem not in {task,health.stem,schedule.stem})
                selected_request=json.loads(selected_link.read_text())
                assert selected_request['operation']=='archive_series_url'
                assert selected_request['args']['url']=='https://tapas.io/series/chosen/info'
                assert selected_request['args']['limit']==1
                assert selected_request['args']['download_workers']==2
                assert selected_request['args']['speed']=='fast'
                if not local:
                    assert registry.storage('synthetic-alice',alice.id)['image_profile']=='small'
                private_json(responses/selected_link.name,{'task_id':selected_link.stem,'status':'ok','result':{
                    'status':'limited','archived_episode_ids':['81'],'downloaded_pages':2,'downloaded_bytes':1}})
                selected_link.unlink()
                page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('1 episodes saved')
                page.locator('#archive').click()
                expect(page.locator('#notice')).to_contain_text('Import queued')
                refused=next(path for path in requests.glob('*.json') if path.stem not in {task,health.stem,schedule.stem})
                private_json(responses/refused.name,{'task_id':refused.stem,'status':'error',
                    'error_code':'source_refused','error':'PRIVATE_RESPONSE_BODY',
                    'result':{'pause_reason':'source_refused','source_http_status':403}})
                refused.unlink();page.evaluate('poll()')
                expect(page.locator('#notice')).to_contain_text('Tapas refused a request')
                expect(page.locator('#notice')).to_contain_text('usual browser before resuming')
                assert 'PRIVATE_RESPONSE_BODY' not in page.locator('body').inner_text()
                denied=page.evaluate('async path => (await fetch(path)).status','/api/v1/workspaces/'+bob.id+'/series')
                assert denied==404
                held=threading.Event();release=threading.Event()
                def hold_import_lock():
                    with LibraryLock(alice.library):
                        held.set();release.wait(30)
                writer=threading.Thread(target=hold_import_lock);writer.start()
                try:
                    assert held.wait(5)
                    page.locator('#downloads').click()
                    expect(page.locator('#files tr')).to_have_count(3)
                    original=next(alice.library.glob('*/*.cbz'))
                    row=page.locator('#files tr').filter(has_text='.cbz');button=row.locator('.file-action')
                    expect(button).to_have_text('Open' if local else 'Download')
                    if local:
                        with patch('comic_archive.local_web.subprocess.Popen') as launch:
                            launch.return_value.wait.return_value=0
                            button.click()
                            expect(row.locator('.file-action-status')).to_have_text('Sent to your device’s default app.')
                            assert launch.call_args.args[0]==['xdg-open',str(original)]
                        # The unchanged authenticated download API still serves
                        # verified bytes, though Open uses the existing path.
                        cbz=next(value for value in registry.exports(LOCAL_OWNER,alice.id) if value['kind']=='cbz')
                        data=page.evaluate('async path => Array.from(new Uint8Array(await (await fetch(path)).arrayBuffer()))',
                            '/api/v1/workspaces/'+alice.id+'/exports/'+cbz['id'])
                        assert bytes(data)==original.read_bytes()
                    else:
                        with page.expect_download() as event: button.click()
                        download=event.value
                        assert download.suggested_filename==original.name
                        assert Path(download.path()).read_bytes()==original.read_bytes()
                finally:
                    release.set();writer.join(5)
                assert not writer.is_alive()
                page.goto(site+'/')
                expect(page.locator('#notice')).to_have_text('Library loaded. Choose a series to get started.')
                page.locator('#advanced > summary').click()
                page.locator('#audit').click()
                expect(page.locator('#audit-status')).to_contain_text('1 inventoried episodes verified')
                with page.expect_download() as event:
                    page.locator('#audit-download').click()
                audit=json.loads(Path(event.value.path()).read_text())
                assert audit['verified_inventory_episode_count']==1 and audit['account_archive_complete'] is False
                assert 'account_binding' not in audit and 'account_salt' not in audit
                # Exercise a run-scoped stop without terminating a worker or
                # introducing browser/source traffic. The real engine's cache
                # retention and resume are tested separately with a live thread.
                with Inventory(alice.library) as inventory:run=inventory.start_run()
                private_json(alice.library/'.archive-state/progress.json',{
                    'run_id':run,'status':'running','cancellation_supported':True,'phase':'scan',
                    'series_checks_completed':2,'successful_series_checks':1,'held_series_checks':1,'series_check_target_count':5})
                page.evaluate('poll()')
                expect(page.locator('#progress')).to_contain_text('2 of 5 checks completed this batch')
                expect(page.locator('#progress')).to_contain_text('1 held checks')
                expect(page.locator('#account-progress')).to_contain_text('2 of 5 checks completed this batch')
                # Reload must preserve the visible server progress and prevent
                # accidentally starting a duplicate batch from an empty browser state.
                private_json(alice.session/'worker/status.json', {'state':'busy','task_id':'job-'+'a'*24})
                request_count = len(list(requests.glob('*.json')))
                page.reload()
                page.locator('#advanced > summary').click()
                expect(page.locator('#account-progress')).to_contain_text('2 of 5 checks completed this batch')
                expect(page.locator('#watch')).to_have_text('Checking known series…')
                expect(page.locator('#watch')).to_be_disabled()
                expect(page.locator('#preview-series')).to_be_disabled()
                expect(page.locator('#next-step')).to_contain_text('A batch is already running')
                expect(page.locator('#notice')).to_contain_text('still running')
                assert len(list(requests.glob('*.json'))) == request_count

                private_json(alice.library/'.archive-state/progress.json',{
                    'run_id':run,'status':'running','cancellation_supported':True,'phase':'metadata'})
                page.evaluate('poll()')
                expect(page.locator('#progress')).to_contain_text('Updating series details and cover')
                expect(page.locator('#stop')).to_be_enabled()
                page.locator('#stop').click()
                expect(page.locator('#progress')).to_contain_text('Stop requested')
                expect(page.locator('#stop')).to_be_disabled()
                marker=alice.library/'.archive-state/cancellations'/f'{run}.json'
                assert json.loads(marker.read_text())=={'document_type':'comic_import_stop','run_id':run}
                assert not list(bob.library.glob('.archive-state/cancellations/*'))
                with Inventory(alice.library) as inventory:inventory.finish_run(run,'cancelled',{})
                private_json(alice.library/'.archive-state/progress.json',{'run_id':run,'status':'cancelled'})
                private_json(alice.session/'worker/status.json', {'state':'ready'})
                page.evaluate('poll()')
                expect(page.locator('#progress')).to_contain_text('Batch stopped')
                expect(page.locator('#watch')).to_be_enabled()
                expect(page.locator('#account-progress')).to_have_attribute('aria-busy', 'false')
                if screenshot:
                    # Reload resets collapsed sections; open the search before
                    # clearing it for the final screenshot.
                    page.locator('#series-options').evaluate('node => node.open = true')
                    page.locator('#search').fill('')
                    page.locator('#advanced').evaluate('node => node.open = false')
                    page.evaluate("history.replaceState(null, '', location.pathname + location.search); showPage()")
                    page.locator('#series-options').evaluate('node => node.open = false')
                    page.screenshot(path=str(screenshot),full_page=True)
                saved_library_controls(context, site, registry, alice, local, screenshot)
                if local: own_window_controls(context, site, registry, alice)
                context.close()
            finally:browser.close()
        return {'browser':'firefox','offline':True,'source_requests':0,'owner_isolation':True,
                'mode':'local' if local else 'hosted-component','local_signed_session':local,
                'title_html_not_executed':True,'selected_series_queue':True,'session_health':True,
                'inline_connection_check_feedback':True,
                'daily_free_only_schedule':True,'verified_cbz_download':True,'audit_report_download':True,
                'scan_freshness_display':True,
                'unlocked_observation_filter':True,
                'direct_series_link_import':True,
                'guided_first_download':local,
                'simple_account_go':True,
                'account_list_live_counts_and_stall_feedback':True,
                'theme_device_default_toggle_reload_and_saved_files':True,
                'shared_navigation_both_views_simple_and_advanced':True,
                'browser_only_tapas_signin':local,
                'hidden_options_retained':True,
                'desktop_local_destination':True,
                'cloud_transfer_experiment_available':local,
                'advanced_selection_controls_hidden':True,
                'run_scoped_stop':True,
                'queued_stop':True,
                'gateway_pause_display':True,
                'completed_series_reuse_notice':True,
                'metadata_retry_notice':True,
                'metadata_phase_display':True,
                'completed_scan_counts':True,
                'separate_scan_total_duration':True,
                'saved_file_table_and_pagination':True,
                'saved_file_action_buttons':True,
                'device_file_open_without_copy':local,
                'requests_checked':len(calls)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot',type=Path)
    parser.add_argument('--mobile',action='store_true',help='Check the narrow viewport layout')
    parser.add_argument('--local',action='store_true',help='Exercise loopback session bootstrap and reuse the local project layout')
    args=parser.parse_args()
    try:print(json.dumps(smoke(args.screenshot,args.mobile,args.local),indent=2))
    except Exception as error:
        print('Offline dashboard smoke test failed ('+type(error).__name__+').')
        # Keep failure locations useful without logging page contents or
        # unfiltered browser errors. Fixtures deliberately contain private fields.
        print(json.dumps({'frames': [{'function':frame.name, 'line':frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)
            if Path(frame.filename).name == 'dashboard_smoke.py']}))
        raise SystemExit(1)
