import argparse
import json
import sys
from pathlib import Path
from .publication import PublicationStore as Store
from .archive import verify
from .adapters.local import LocalAdapter
from .models import ArchiveError


def parser():
    from .state_paths import session_path
    p = argparse.ArgumentParser(prog="comic-archive", description="Comic Archiver: personal comic CBZ and novel EPUB preservation")
    p.add_argument("--session-dir", type=Path, default=Path(session_path(Path.cwd()).name))
    p.add_argument("--browser", choices=("auto", "chromium", "firefox", "qt"), default="auto",
                   help="Browser engine; auto falls back from Chromium to Firefox, qt is the desktop app's built-in engine")
    p.add_argument("--headless", action="store_true", help="Run authenticated imports without a browser window")
    p.add_argument("--request-interval", type=float, default=1.0, help="Seconds between image requests (0.25–60; default 1; metadata pacing is separate)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_text in (
            ('cloud-connect','Sign in locally to Google Drive/Dropbox, or adopt a private rclone connection'),
            ('cloud-test','Upload one to three completed episodes, verify them, then delete the cloud test files'),
            ('cloud-enable','Upload future completed episodes and remove their local files after verification')):
        q = sub.add_parser(name, help=help_text)
        q.add_argument('provider', choices=('google','dropbox','proton','b2'))
        q.add_argument('--output', type=Path, default=Path('Library'))
        if name == 'cloud-connect':
            q.add_argument('--remote', help='Existing rclone connection name; use with --config')
            q.add_argument('--config', type=Path, help='Existing private rclone configuration')
            q.add_argument('--client-json', type=Path, help='Maintainer setup: Google Desktop or Dropbox app registration JSON')
        elif name == 'cloud-test':
            q.add_argument('--episodes', type=int, default=2)
            q.add_argument('--stream', action='store_true', help='Test the Drive/Dropbox streaming upload pipe and full readback')
        else:
            q.add_argument('--disable', action='store_true')
    q = sub.add_parser('cloud-status', help='Show upload/cleanup status without listing cloud storage')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q = sub.add_parser('cloud-upload', help='Retry pending uploads, or explicitly queue a small number of existing episodes')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--provider', choices=('google','dropbox','proton','b2'))
    q.add_argument('--limit', type=int, default=3)
    q.add_argument('--existing', action='store_true')
    q = sub.add_parser('cloud-cleanup', help='Delete only the exact files journaled by a previous small cloud test')
    q.add_argument('test_id')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q = sub.add_parser('storage-plan', help='Estimate known library storage against free space on this device')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--episodes', type=int, help='Optional planned episode count before discovery')
    q.add_argument('--content-type', choices=('comic','novel'), default='comic')
    q = sub.add_parser('preview-series', help='Check series access and device space without downloading episodes')
    q.add_argument('url')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--scope', choices=('one','series'), default='series')
    q = sub.add_parser('storage-policy', help='Choose original or smaller lossy comic images for future imports')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--image-profile', choices=('original','balanced','small'), default='original')
    q.add_argument('--keep-completed-cache', action='store_true')
    q = sub.add_parser('network-policy', help='Check or choose Wi-Fi-only transfers (unmetered wired is also allowed)')
    q.add_argument('--output', type=Path, default=Path('Library'))
    group = q.add_mutually_exclusive_group()
    group.add_argument('--wifi-only', action='store_true')
    group.add_argument('--allow-metered', action='store_true', help='Explicitly allow cellular, metered and unverified connections')
    q = sub.add_parser('convert-comics', help='Convert existing tar.zst comics to standard CBZ without source requests')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--limit', type=int)
    q.add_argument('--remove-originals', action='store_true', help='Remove each tar only after the CBZ is verified')
    q.add_argument('--reserve-bytes', type=int, default=256*1024**2)
    q = sub.add_parser('purge', help='Verify and remove redundant cached media; retain incomplete data and indexes')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--apply', action='store_true', help='Remove verified redundant files; default only reports candidates')
    q.add_argument('--include-legacy', action='store_true', help='Also remove older exports with verified equivalent replacements')
    q.add_argument('--wait-for-lock', type=float, default=0)
    q = sub.add_parser('serve-local', help='Open a loopback website for this project’s existing library and browser worker')
    q.add_argument('--project-root', type=Path, default=Path.cwd(), help='Project containing Library and .comic-session (default: current directory)')
    q.add_argument('--port', type=int, default=8765, help='Local port (default: 8765)')
    q = sub.add_parser('audit', help='Reconcile recorded access and verify portable files without contacting Tapas')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--report', type=Path, help='Save the dated reconciliation as JSON')
    q.add_argument('--json', action='store_true', help='Print the complete reconciliation, including outstanding episodes')
    q = sub.add_parser('backup', help='Incrementally copy verified CBZ/EPUB/JSON/covers without sessions or cache')
    q.add_argument('destination', type=Path)
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--wait-for-lock', type=float, default=0, help='Seconds to wait for the current import to finish')
    q.add_argument('--reserve-bytes', type=int, default=1024**3, help='Minimum destination free space to retain')
    q = sub.add_parser('convert-exports', help='Convert legacy exports to neutral CBZ/EPUB and remove public source identifiers; retain originals privately')
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--limit', type=int, help='Maximum saved episodes to convert this invocation')
    q.add_argument('--reserve-bytes', type=int, default=256*1024*1024)
    q = sub.add_parser('queue-priority', help='Choose expiring grants first (default), or permanent unlocked episodes first')
    q.add_argument('priority', choices=('expiring_first','owned_first'))
    q.add_argument('--output', type=Path, default=Path('Library'))
    q.add_argument('--via-worker', action='store_true')
    login = sub.add_parser("login", help="Log in using a visible browser or import Firefox cookies")
    login.add_argument("--firefox-profile", type=Path, help="Import Tapas cookies from this existing Firefox profile")
    sub.add_parser("doctor", help="Test browser startup without account data or network requests")
    worker=sub.add_parser("browser-worker", help="Run a local browser worker for assistant-controlled inspection tasks")
    worker.add_argument('--stable-code',action='store_true',help='Keep the loaded adapter code until restart; use for supervised hosted workers')
    q = sub.add_parser('start-worker', help='Start one headless background worker for this project')
    q.add_argument('--resume-backlog',action='store_true',help='Also resume the saved controller in the background, retaining its original budgets/deadline')
    q.add_argument('--restart',action='store_true',help='Finish the current task and restart to load updated code; waits up to 30 seconds')
    sub.add_parser('worker-status', help='Check the worker runtime lock and background status')
    q=sub.add_parser('stop-worker', help='Finish the current task, then stop the worker; keep queued jobs')
    q.add_argument('--wait',type=float,default=0,help='Wait up to this many seconds for the worker to drain (default: return immediately)')
    q = sub.add_parser('test-background', help='Test ordinary HTTP session access and headless episode archiving through the worker')
    q.add_argument('url', help='One known accessible Tapas episode URL')
    q=sub.add_parser('daily-schedule',help='Configure free-only daily runs in the existing browser worker')
    q.add_argument('--disable',action='store_true')
    q.add_argument('--run-now',action='store_true',help='Run once when the worker is idle, then follow the daily schedule')
    q.add_argument('--at',default='08:00')
    q.add_argument('--timezone',default='America/Chicago')
    q.add_argument('--limit',type=int,default=50)
    q.add_argument('--max-pages',type=int,default=2000)
    q.add_argument('--max-bytes',type=int,default=1024**3)
    q.add_argument('--max-seconds',type=int,default=3600)
    q.add_argument('--scan-limit',type=int,help='Optional rotating subset; default attempts every watched series')
    sub.add_parser('daily-status',help='Read the private daily schedule and last scheduled result')
    q=sub.add_parser('run-backlog',help='Run consecutive bounded sync batches through the existing browser worker')
    q.add_argument('--speed',choices=('normal','fast'),default='normal',help='Fast saves the known queue with --archive-only')
    q.add_argument('--download-workers',type=int,choices=(1,2,3,4),default=1,help='Image transfers per batch; aggregate request spacing is unchanged')
    q.add_argument('--resume',action='store_true',help='Observe the saved pending task and retain the original budgets')
    q.add_argument('--max-batches',type=int,default=4)
    q.add_argument('--max-pages',type=int,default=8000,help='Total new comic pages or novel chapters across these batches')
    q.add_argument('--max-bytes',type=int,default=4*1024**3,help='Total image bytes across these batches')
    q.add_argument('--max-seconds',type=int,default=7200,help='Submission window; an already queued batch may finish afterward')
    q.add_argument('--batch-limit',type=int,default=100)
    q.add_argument('--batch-pages',type=int,default=2000)
    q.add_argument('--batch-bytes',type=int,default=1024**3)
    q.add_argument('--batch-seconds',type=int,default=1800)
    q.add_argument('--scan-limit',type=int,default=50)
    q.add_argument('--free-only',action='store_true')
    q.add_argument('--watched-only',action='store_true',help='Skip the first-batch account-list refresh')
    q.add_argument('--archive-only',action='store_true',help='Archive recorded candidates without list/series refresh; every episode still receives fresh authorization')
    q.add_argument('--recover-network',action='store_true',help='Allow at most two network recovery batches after 60/120-second waits, within the same budgets and deadline')
    q.add_argument('--no-source-recovery',dest='recover_source',action='store_false',help='Hold immediately on reader refusals instead of using bounded adaptive cooldowns')
    q.add_argument('--source-recovery-limit',type=int,default=4,help='Maximum automatic reader recovery probes in this window (0–4)')
    sub.add_parser('backlog-status',help='Read consecutive-import budgets, totals and the pending task')
    sub.add_parser('stop-backlog',help='Stop further consecutive submissions; retain an existing pending task')
    q=sub.add_parser('complete-backlog',help='Continue the known queue across bounded windows while the worker runs')
    q.add_argument('--speed',choices=('normal','fast'),default='normal',help='Speed for new windows; --resume retains the saved setting')
    q.add_argument('--download-workers',type=int,choices=(1,2,3,4),default=1,help='Image transfers for new windows; --resume keeps the saved setting')
    q.add_argument('--resume',action='store_true',help='Retain the saved plan and deadline; held windows must be explicitly repaired first')
    q.add_argument('--until-drained',action='store_true',help='Remove overall day/window/page/byte caps; retain bounded jobs, accounting and recovery holds')
    q.add_argument('--max-windows',type=int,default=7)
    q.add_argument('--max-days',type=int,default=7)
    q.add_argument('--max-pages',type=int,default=700000)
    q.add_argument('--max-bytes',type=int,default=448*1024**3)
    q.add_argument('--max-network-recoveries',type=int,default=4)
    q.add_argument('--max-source-recoveries',type=int,default=8,help='Maximum automatic reader recovery probes across all windows (0–8)')
    q.add_argument('--no-source-recovery',dest='recover_source',action='store_false',help='Hold immediately on reader refusals in new windows')
    sub.add_parser('completion-status',help='Read the overall known-backlog continuation plan and conservative totals')
    sub.add_parser('stop-completion',help='Stop window renewal and further backlog submissions; keep an existing batch')
    q = sub.add_parser('extract-account', help='Refresh provided reading lists and watched series, extract accessible episodes, then audit the files')
    q.add_argument('--resume', action='store_true', help='Observe the saved export and its existing pending task')
    q.add_argument('--speed', choices=('normal', 'fast'), default='normal')
    q.add_argument('--download-workers', type=int, choices=(1, 2, 3, 4), default=1)
    sub.add_parser('account-status', help='Read account discovery, extraction and final audit progress')
    sub.add_parser('stop-account', help='Stop further account-export submissions; keep existing tasks')
    q=sub.add_parser('enrich-series',help='Preserve public creator/description/credits and original cover bytes')
    q.add_argument('id')
    q.add_argument('--output',type=Path,default=Path('Library'))
    q.add_argument('--refresh',action='store_true',help='Fetch the cover again instead of using verified cache')
    q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker (fixed Library output)')
    q = sub.add_parser("inspect-episode", help="Automatically inspect reader sources and access; signed URLs are redacted")
    q.add_argument("url")
    q.add_argument("--details", action="store_true", help="Include redacted browser/network diagnostics")
    q.add_argument('--metadata',action='store_true',help='Inspect authorization flags and hold reasons without opening a reader')
    q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
    q = sub.add_parser('inspect-history',help='Read normal transaction-history pages without recording balances or making purchases')
    q.add_argument('--max-pages',type=int,default=3)
    q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
    q.add_argument('--resume',action='store_true',help='Continue the live worker history tab; requires --via-worker')
    q=sub.add_parser('inspect-free-access',help='Inspect normal Home/Free Access navigation without guessing menu IDs or authorizing downloads')
    q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
    for name in ("inspect-library", "inspect-series"):
        q = sub.add_parser(name, help="Inspect the current account or enumerate a series")
        if name == "inspect-series":
            q.add_argument("id", help="Series ID or slug")
            q.add_argument('--metadata',action='store_true',help='Inspect public creator/description/cover structure instead of episode enumeration')
            q.add_argument('--page-size',type=int,choices=(20,50,100),help='Probe one metadata page at this size; opens no readers or comic images')
            q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
        else:
            q.add_argument("--details", action="store_true", help="Include browser/network diagnostics")
            q.add_argument("--category", choices=("subscribed", "recent", "updated"), default="subscribed")
            q.add_argument('--content-type',choices=('all','comics','novels'),help='Inspect the normal reading-list filter, then restore its original selection')
            q.add_argument('--pages',type=int,help='Follow supplied All reading-list pager links, up to this limit (1–200)')
            q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
    q = sub.add_parser("discover-available", help="Read-only access discovery across account reading-list tabs; never unlocks or purchases")
    q.add_argument("--category", choices=("subscribed", "recent", "updated", "free_episodes"), action="append",
                   help="Choose a view; repeat for multiple views (default: subscribed, recent, updated; free_episodes is optional)")
    q.add_argument("--report", type=Path, default=Path("Library/discovery.json"))
    q.add_argument("--resume", action="store_true", help="Reuse dated completed observations; omit to refresh after account/access changes")
    for name in ("archive-discovered", "sync", "import-series"):
        q = sub.add_parser(name, help="Import a chosen series without scanning account lists" if name == "import-series" else "Archive known accessible episodes" if name == "archive-discovered" else "Refresh watched access and archive missing episodes")
        if name != 'sync':
            q.add_argument('--speed', choices=('normal', 'fast'), default='normal',
                           help='Fast uses a fixed image pool and overlaps fresh metadata checks')
        q.add_argument('--download-workers', type=int, choices=(1, 2, 3, 4), default=1,
                       help='Concurrent image transfers sharing one request limit (default: 1)')
        q.add_argument("--output", type=Path, default=Path("Library"))
        q.add_argument("--report", type=Path, help="Seed candidates from a discovery report; every download rechecks access")
        q.add_argument("--limit", type=int, help="Maximum queued episodes to attempt")
        q.add_argument("--max-pages", type=int, help="Maximum new comic pages or novel chapters this run")
        q.add_argument("--max-bytes", type=int, help="Maximum image bytes this run")
        q.add_argument("--max-seconds", type=float, help="Stop between operations after this duration")
        q.add_argument("--free-only", action="store_true", help="Process only currently free episodes")
        q.add_argument("--json", action="store_true", help="Print the complete structured job result")
        q.add_argument("--via-worker", action="store_true", help="Queue in the existing normal-terminal browser worker (fixed Library output)")
        q.add_argument("--wait-for-lock",type=float,default=0,help="Wait this many seconds for another library writer before opening a browser")
        if name == 'import-series':
            q.add_argument('url', help='Public https://tapas.io/series/... link')
            q.set_defaults(limit=1, max_pages=1000, max_bytes=256*1024**2, max_seconds=900, download_workers=2)
        if name == "sync":
            q.add_argument('--scan-only',action='store_true',help='Refresh inventory without opening readers or downloading assets')
            q.add_argument("--category", choices=("subscribed", "recent", "updated", "free_episodes"), action="append",
                           help='Choose a view; free_episodes explicitly discovers candidates outside the known library')
            q.add_argument('--include-history',action='store_true',help='Include bounded transaction-history series discovery')
            q.add_argument('--history-pages',type=int,default=100,help='Maximum history pages to observe (1–500; default 100)')
            q.add_argument('--history-resume',action='store_true',help='Continue a live history scan; requires --include-history --via-worker')
            q.add_argument('--scan-limit',type=int,help='Refresh at most this many watched series; resume the scan cycle next sync')
            q.add_argument('--watched-only',action='store_true',help='Refresh observed series without revisiting account lists; omit periodically to discover additions')
    q = sub.add_parser('check-session',help='Check current account authentication and library binding without opening episodes')
    q.add_argument('--output',type=Path,default=Path('Library'))
    q.add_argument('--via-worker',action='store_true',help='Use the existing browser worker')
    q = sub.add_parser("task-status", help="Read a browser-worker task result without opening a browser")
    q.add_argument("task_id")
    q = sub.add_parser('stop-task',help='Stop an import queued for a supporting worker before it starts')
    q.add_argument('task_id')
    for name in ("import-local", "archive-series", "archive-library"):
        q = sub.add_parser(name)
        if name == "import-local":
            q.add_argument("manifest", type=Path)
        else:
            if name == "archive-series":
                q.add_argument("id")
                q.add_argument("--limit", type=int, help="Maximum accessible episodes to import")
            q.add_argument("--manifest", type=Path, help="Explicit ordered local-files manifest")
        q.add_argument("--output", type=Path, default=Path("Library"))
        q.add_argument("--refresh", action="store_true", help="Re-read sources instead of using resume cache")
    q = sub.add_parser("archive-episode", help="Archive one free/already-unlocked episode without manual scrolling")
    q.add_argument('--download-workers', type=int, choices=(1, 2, 3, 4), default=1,
                   help='Concurrent image transfers sharing one request limit (default: 1)')
    q.add_argument("url")
    q.add_argument("--series-id")
    q.add_argument("--series-title")
    q.add_argument("--title")
    q.add_argument("--order", type=int)
    q.add_argument("--manual", action="store_true", help="Explicit full-reader confirmation fallback; requires all metadata options")
    q.add_argument("--http-only", action="store_true", help="Try metadata/HTML without a browser; stops if normal HTTP access is refused")
    q.add_argument("--page-selector", help="Override the observed default #episode-<id> img.content__img selector")
    q.add_argument("--output", type=Path, default=Path("Library"))
    q.add_argument("--refresh", action="store_true")
    q = sub.add_parser("verify", help="Verify page membership, order, size, and SHA-256")
    q.add_argument("library", type=Path)
    q.add_argument('--decode-images',action='store_true',help='Also decode every saved page and cover locally (requires Pillow; no downloads)')
    return p


def run(args):
    if args.command.startswith('cloud-'):
        from . import cloud
        if args.command == 'cloud-connect':
            if bool(args.config) != bool(args.remote):
                raise ArchiveError('Use --config and --remote together for an existing connection.')
            if args.client_json:
                if args.provider not in cloud.OAUTH_PROVIDERS or args.config:
                    raise ArchiveError('Use --client-json only with a new Google or Dropbox connection.')
                from . import google_client, dropbox_client
                registration = google_client if args.provider == 'google' else dropbox_client
                registration.install(args.output, args.client_json)
            if args.provider == 'proton':
                print('This is a third-party application not officially supported by Proton. Its rclone adapter is experimental.', file=sys.stderr)
            result = (cloud.configure_target(args.output,args.provider,config=args.config,remote=args.remote)
                      if args.config else cloud.connect(args.output,args.provider))
        elif args.command == 'cloud-test':
            options = {'episodes': args.episodes}
            if args.stream:
                options['stream'] = True
            result = cloud.smoke_test(args.output,args.provider,**options)
        elif args.command == 'cloud-enable':result = cloud.enable(args.output,args.provider,not args.disable)
        elif args.command == 'cloud-status':result = cloud.status(args.output)
        elif args.command == 'cloud-cleanup':result = cloud.cleanup_test(args.output,args.test_id)
        else:
            result = (cloud.upload_existing(args.output,limit=args.limit,provider=args.provider) if args.existing
                      else cloud.upload_pending(args.output,limit=args.limit,provider=args.provider))
        print(json.dumps(result,indent=2))
        return 0
    if args.command == 'storage-plan':
        from .storage import storage_plan
        print(json.dumps(storage_plan(args.output, episode_count=args.episodes, episode_kind=args.content_type), indent=2))
        return 0
    if args.command == 'preview-series':
        from .preflight import preview_series, validate_options
        validate_options({'url':args.url,'scope':args.scope})
        from .adapters.tapas import TapasAdapter
        with TapasAdapter(args.session_dir,args.browser,headless=args.headless,interval=args.request_interval) as adapter:
            print(json.dumps(preview_series(adapter,args.url,args.output,scope=args.scope),indent=2))
        return 0
    if args.command == 'network-policy':
        from .network import configure, status
        result = configure(args.output, args.wifi_only) if args.wifi_only or args.allow_metered else status(args.output)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == 'convert-comics':
        from .cbz_conversion import convert_comics
        result = convert_comics(args.output, limit=args.limit, remove_originals=args.remove_originals,
                                min_free_bytes=args.reserve_bytes,
                                progress=lambda value: print(json.dumps(value),file=sys.stderr,flush=True))
        print(json.dumps(result,indent=2))
        return 0
    if args.command == 'storage-policy':
        from .storage import configure
        print(json.dumps(configure(args.output, image_profile=args.image_profile,
                                   purge_completed_cache=not args.keep_completed_cache), indent=2))
        return 0
    if args.command == 'purge':
        from .purge import purge_library
        result = purge_library(args.output, apply=args.apply, include_legacy=args.include_legacy,
                               wait_seconds=args.wait_for_lock,
                               progress=lambda value: print(json.dumps({'verified_archives': value['verified_archives']}), file=sys.stderr, flush=True))
        print(json.dumps(result, indent=2))
        return 0
    if args.command in {'start-worker', 'worker-status', 'stop-worker'}:
        from . import background
        if args.command == 'start-worker':
            result = background.start(args.session_dir, args.browser, args.request_interval,
                                      resume=args.resume_backlog,restart=args.restart)
        elif args.command == 'stop-worker':
            result = background.stop(args.session_dir,wait_seconds=args.wait)
        else:
            result = background.status(args.session_dir)
        print(json.dumps(result, indent=2))
        return 1 if (result.get('start_failed') or result.get('start_deferred')
                     or result.get('backlog_resume_failed') or result.get('stop_wait_expired')) else 0
    if args.command in {'extract-account', 'account-status', 'stop-account'}:
        from . import account_export
        if args.command == 'account-status':
            result = account_export.status(args.session_dir)
        elif args.command == 'stop-account':
            result = account_export.request_stop(args.session_dir)
        else:
            values = {'request_interval': args.request_interval, 'speed': args.speed,
                      'download_workers': args.download_workers}
            if args.speed == 'fast':
                values['batch_bytes'] = 8 * 1024**3
            result = account_export.configure(args.session_dir, resume=args.resume, window_budgets=values)
        print(json.dumps(result, indent=2))
        return 1 if result.get('status') == 'held' else 0
    if args.command in {'complete-backlog','completion-status','stop-completion'}:
        from . import completion
        if args.command=='completion-status':result=completion.status(args.session_dir)
        elif args.command=='stop-completion':result=completion.request_stop(args.session_dir)
        else:
            values={key:getattr(args,key) for key in completion.DEFAULT_LIMITS}
            window_budgets={'request_interval':args.request_interval}
            if not args.recover_source:window_budgets['recover_source']=False
            if args.speed == 'fast':window_budgets.update(speed='fast',batch_bytes=8*1024**3)
            if args.download_workers!=1:window_budgets['download_workers']=args.download_workers
            result=completion.configure(args.session_dir,values=values,resume=args.resume,
                                        window_budgets=window_budgets,until_drained=args.until_drained)
        print(json.dumps(result,indent=2));return 1 if result.get('status')=='paused' else 0
    if args.command == 'test-background':
        from .queue_client import submit_inspection
        result = submit_inspection(args.session_dir, 'diagnose_headless', {'url':args.url})
        print(json.dumps(result, indent=2))
        print(f"Read the result with: comic-archive task-status {result['task_id']}")
        return 0
    if args.command in {'run-backlog','backlog-status','stop-backlog'}:
        from . import batches
        from .state_paths import session_path
        if args.session_dir.absolute() != session_path(Path.cwd()):
            raise ArchiveError('Consecutive imports use this project’s .comic-session and Library.')
        if args.command=='backlog-status':result=batches.status(args.session_dir)
        elif args.command=='stop-backlog':result=batches.request_stop(args.session_dir)
        else:
            values={key:getattr(args,key) for key in ('max_batches','max_pages','max_bytes','max_seconds',
                'batch_limit','batch_pages','batch_bytes','batch_seconds','scan_limit','free_only','recover_network','recover_source','source_recovery_limit')}
            values.update(request_interval=args.request_interval,download_workers=args.download_workers,speed=args.speed,
                          refresh_lists=not (args.watched_only or args.archive_only),archive_only=args.archive_only)
            print('Consecutive imports use the existing worker. Ctrl+C stops the controller; a pending bounded batch is retained.',flush=True)
            result=batches.run(args.session_dir,values=values,resume=args.resume,
                               progress=lambda line:print(line,flush=True))
        print(json.dumps(result,indent=2))
        return 1 if result.get('status')=='paused' else 0
    if args.command == 'audit':
        from .audit import audit_library
        result = audit_library(args.output, args.report)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Verified {result['verified_inventory_episode_count']} inventoried episodes; {result['missing_recorded_accessible_episode_count']} recorded accessible episodes still need verified archives.")
            print(f"Current scan cycle: {result['scan_remaining_series_count']} series remaining. Account coverage is unverified.")
            print('Portable file integrity: ' + ('passed' if result['archive_integrity_passed'] else 'requires repair'))
            if args.report is not None: print('Dated reconciliation saved as JSON.')
        return 0 if result['archive_integrity_passed'] else 1
    if args.command == 'queue-priority':
        if args.via_worker:
            if args.output != Path('Library'): raise ArchiveError('Worker queue order uses this project\'s Library.')
            from .queue_client import submit_inspection
            result=submit_inspection(args.session_dir,'configure_priority',{'priority':args.priority})
        else:
            from .inventory import Inventory
            with Inventory(args.output) as inventory:
                result={'queue_priority':inventory.set_queue_priority(args.priority)}
        print(json.dumps(result,indent=2))
        return 0
    if args.command == 'convert-exports':
        from .migration import migrate_exports
        result = migrate_exports(args.output, limit=args.limit, min_free_bytes=args.reserve_bytes,
                                 progress=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(result, indent=2))
        return 0
    if args.command == 'backup':
        from .backup import backup_library
        print('Copying verified portable exports only; an active writer must finish first.', flush=True)
        result = backup_library(args.output, args.destination, wait_seconds=args.wait_for_lock,
                                reserve_bytes=args.reserve_bytes,
                                progress=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(result, indent=2))
        return 0
    if args.command == 'serve-local':
        from .state_paths import session_path
        if args.session_dir.absolute() != session_path(Path.cwd()):
            raise ArchiveError('Local website uses .comic-session in its project directory; use --project-root.')
        if not 1 <= args.port <= 65535:
            raise ArchiveError('Local dashboard port must be between 1 and 65535.')
        from .local_web import serve
        serve(args.project_root, args.port)
        return 0
    if args.command=='inspect-free-access':
        if args.via_worker:
            from .queue_client import submit_inspection
            result=submit_inspection(args.session_dir,'inspect_free_access')
            print(f"Queued {result['task_id']}. Keep browser-worker running.")
            print(f"Read the result with: comic-archive task-status {result['task_id']}")
        else:
            from .adapters.tapas import TapasAdapter
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                print(json.dumps(adapter.inspect_free_access(),indent=2))
        return 0
    if args.command=='enrich-series':
        if args.via_worker:
            if args.output!=Path('Library'):
                raise ArchiveError('The browser worker uses Library; omit output overrides.')
            from .queue_client import submit_job
            result=submit_job(args.session_dir,'enrich_series',{'id':args.id,'refresh':args.refresh})
            print(f"Queued {result['task_id']}. Keep browser-worker running.")
            print(f"Read the result with: comic-archive task-status {result['task_id']}")
        else:
            from .metadata import enrich_series
            from .adapters.tapas import TapasAdapter
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                print(json.dumps(enrich_series(adapter,args.id,args.output,args.refresh),indent=2))
        return 0
    if args.command=='inspect-series' and args.page_size is not None:
        if args.metadata:
            raise ArchiveError('Choose series metadata or a page-size experiment.')
        if args.via_worker:
            from .queue_client import submit_inspection
            result=submit_inspection(args.session_dir,'inspect_episode_page_size',{'id':args.id,'max_limit':args.page_size})
        else:
            from .adapters.tapas import TapasAdapter
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                result=adapter.inspect_episode_page_size(args.id,args.page_size)
        print(json.dumps(result,indent=2))
        return 0
    if args.command=='inspect-series' and args.via_worker:
        from .queue_client import submit_inspection
        options={'id':args.id} if args.metadata else {'id':args.id,'strict_order':False}
        result = submit_inspection(args.session_dir,'inspect_series_metadata' if args.metadata else 'inspect_series',options)
        print(f"Queued {result['task_id']}. Keep browser-worker running.")
        print(f"Read the result with: comic-archive task-status {result['task_id']}")
        return 0
    if (args.command == 'sync' and args.history_resume and not (args.include_history and args.via_worker)):
        raise ArchiveError('History resume needs --include-history --via-worker because its cursor stays in the live browser.')
    if args.command=='check-session':
        if args.via_worker:
            if args.output!=Path('Library'):
                raise ArchiveError('The browser worker checks its fixed Library output; omit output overrides.')
            from .queue_client import submit_inspection
            result=submit_inspection(args.session_dir,'check_session')
        else:
            from .jobs import check_session
            from .adapters.tapas import TapasAdapter
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                result=check_session(adapter,args.output)
        print(json.dumps(result,indent=2))
        return 0
    if args.command=='sync' and args.watched_only and (args.category is not None or args.include_history):
        raise ArchiveError('Watched-only sync cannot also request categories or account history.')
    if args.command == 'inspect-history' and args.resume and not args.via_worker:
        raise ArchiveError('History resume needs --via-worker because a new CLI process has no saved private cursor.')
    if args.command=='inspect-library' and args.pages is not None:
        if not 1<=args.pages<=200 or args.content_type:
            raise ArchiveError('Use --pages from 1 to 200 for All reading-list pagination, without --content-type.')
        if args.via_worker:
            from .queue_client import submit_inspection
            result=submit_inspection(args.session_dir,'inspect_library_pages',{'category':args.category,'max_pages':args.pages})
            print(f"Queued {result['task_id']}. Read it with task-status; keep browser-worker open.")
        else:
            from .adapters.tapas import TapasAdapter
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                print(json.dumps(adapter.inspect_library_pages(args.category,args.pages),indent=2))
        return 0
    if args.command in {'inspect-history','inspect-library'} and args.via_worker:
        from .queue_client import submit_inspection
        operation = 'inspect_transaction_history' if args.command == 'inspect-history' else 'inspect_library'
        options = {'max_pages':args.max_pages,'resume':args.resume} if args.command == 'inspect-history' else {'category':args.category,'details':args.details}
        if args.command=='inspect-library' and args.content_type:
            operation='inspect_library_filter'
            options={'category':args.category,'content_type':args.content_type}
        result = submit_inspection(args.session_dir,operation,options)
        print(f"Queued {result['task_id']}. Keep browser-worker running.")
        print(f"Read the result with: comic-archive task-status {result['task_id']}")
        return 0
    if args.command == "task-status":
        from .queue_client import task_status
        result = task_status(args.session_dir, args.task_id)
        print(json.dumps(result, indent=2))
        return 1 if result['status'] == 'error' else 0
    if args.command=='stop-task':
        from .task_cancellation import request_queued_stop
        print(json.dumps(request_queued_stop(args.session_dir,args.task_id),indent=2))
        return 0
    if args.command in {"archive-discovered", "sync", "import-series"} and args.via_worker:
        if args.output != Path("Library") or args.report is not None or args.wait_for_lock != 0:
            raise ArchiveError("The browser worker uses Library and its existing discovery report; omit output/report overrides.")
        from .queue_client import submit_job
        options = dict(limit=args.limit, max_pages=args.max_pages, max_bytes=args.max_bytes,
                       max_seconds=args.max_seconds, free_only=args.free_only,request_interval=args.request_interval)
        if getattr(args, 'speed', 'normal') == 'fast':
            options['speed'] = 'fast'
        if args.download_workers != 1:
            options['download_workers'] = args.download_workers
        if args.command == 'sync':
            options['categories'] = args.category
            options['include_history'] = args.include_history
            options['history_pages'] = args.history_pages
            options['history_resume'] = args.history_resume
            options['scan_limit'] = args.scan_limit
            if args.scan_only:
                options['scan_only'] = True
            if args.watched_only:
                options['refresh_lists'] = False
        operation = args.command.replace('-', '_')
        if args.command == 'import-series':
            operation = 'archive_series_url'
            options['url'] = args.url
        result = submit_job(args.session_dir, operation, options)
        print(f"Queued {result['task_id']}. Keep browser-worker running.")
        print(f"Read the result with: comic-archive task-status {result['task_id']}")
        return 0
    if args.command == "browser-worker":
        from .worker import run_worker
        run_worker(args.session_dir, args.browser, args.headless, args.request_interval,reload_adapter=not args.stable_code)
        return 0
    if args.command=='daily-schedule':
        from .queue_client import submit_inspection
        options={k:getattr(args,k) for k in ('limit','max_pages','max_bytes','max_seconds')}
        options['request_interval']=args.request_interval
        if args.scan_limit is not None:options['scan_limit']=args.scan_limit
        result=submit_inspection(args.session_dir,'configure_daily',{'enabled':not args.disable,'at':args.at,
            'timezone':args.timezone,'options':options,'run_now':args.run_now})
        print(f"Queued {result['task_id']}. Keep browser-worker open; task-status confirms configuration.")
        return 0
    if args.command=='daily-status':
        from .daily import status
        print(json.dumps(status(args.session_dir),indent=2))
        return 0
    if args.command == "doctor":
        from .session import doctor
        doctor(args.browser, args.headless)
        return 0
    if args.command == "verify":
        errors = verify(args.library,decode_images=args.decode_images)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print("All archives and image pixels verified." if args.decode_images else "All archives verified.")
        return 0
    if args.command == "login":
        if args.headless and not args.firefox_profile:
            raise ArchiveError("Interactive login needs a visible browser; omit --headless.")
        from .session import import_firefox, login
        if args.firefox_profile:
            import_firefox(args.firefox_profile, args.session_dir)
        else:
            login(args.session_dir, args.browser)
    elif args.command in {"archive-discovered", "sync", "import-series"}:
        from .adapters.tapas import TapasAdapter
        from .jobs import archive_discovered, archive_series_url, sync
        from .locking import wait_for_library
        report = args.report
        if args.command == 'import-series' and report is not None:
            raise ArchiveError('A chosen-series import uses its link; omit discovery reports.')
        if report is None and (args.output / "discovery.json").is_file():
            report = args.output / "discovery.json"
        options = dict(limit=args.limit, max_pages=args.max_pages, max_bytes=args.max_bytes,
                       max_seconds=args.max_seconds, free_only=args.free_only)
        speed = getattr(args, 'speed', 'normal')
        if speed == 'fast':
            from .fast_import import FastImportAdapter, fast_options
            options = fast_options(options)
            context = FastImportAdapter(args.session_dir, args.browser, headless=args.headless)
        else:
            context = TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval)
        if speed != 'fast' and args.download_workers != 1:
            options['download_workers'] = args.download_workers
        with wait_for_library(args.output,args.wait_for_lock):
            with context as adapter:
                if args.command == 'import-series':
                    result = archive_series_url(adapter, args.url, args.output, **options)
                elif args.command == 'sync':
                    result = sync(adapter, args.output, report, args.category, include_history=args.include_history,history_pages=args.history_pages,scan_limit=args.scan_limit,refresh_lists=not args.watched_only,scan_only=args.scan_only, **options)
                else:
                    result = archive_discovered(adapter, args.output, report, **options)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            chapters = result.get('downloaded_chapters', 0)
            transfers = f"{result['downloaded_pages'] - chapters} comic pages and {chapters} novel chapters" if chapters else f"{result['downloaded_pages']} pages"
            print(f"Archived {len(result['archived_episode_ids'])} episodes; downloaded {transfers} and {result.get('downloaded_covers',0)} covers ({result['downloaded_bytes'] / 1024 ** 2:.2f} MiB).")
            print(f"Job {result['status'].replace('_', ' ')}; {result.get('pending_episode_count', 0)} matching episodes remain queued.")
            print(f"Details: {args.output / '.archive-state/last-run.json'}")
        return 0
    elif args.command == "discover-available":
        from .adapters.tapas import TapasAdapter
        from .discovery import discover_available
        with TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval) as adapter:
            report = discover_available(adapter, args.report, args.category, args.resume)
        print(json.dumps({key: report[key] for key in ("state", "complete", "scanned_series_count", "scanned_episode_count",
                                                      "free_episode_count", "unlocked_episode_count", "cached_series_count")}
                         | {"report": str(args.report)}, indent=2))
    elif args.command.startswith("inspect-"):
        from .adapters.tapas import TapasAdapter
        if args.command=='inspect-history':
            if not 1 <= args.max_pages <= 500:
                raise ArchiveError('History page limit must be from 1 to 500.')
            with TapasAdapter(args.session_dir,args.browser,args.headless,args.request_interval) as adapter:
                print(json.dumps(adapter.inspect_transaction_history(args.max_pages),indent=2))
            return 0
        if args.command == "inspect-episode":
            if args.metadata and args.details:
                raise ArchiveError('Choose metadata-only inspection or reader details.')
            if args.via_worker:
                from .queue_client import submit_inspection
                operation='inspect_episode_access' if args.metadata else 'inspect_episode'
                options={'url':args.url}
                if args.details:options['details']=True
                print(json.dumps(submit_inspection(args.session_dir,operation,options),indent=2))
                return 0
            with TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval) as adapter:
                result = adapter.inspect_episode_access(args.url) if args.metadata else adapter.inspect_episode(args.url, interactive=False, details=True) if args.details else adapter.inspect_episode(args.url, interactive=False)
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "inspect-series":
            from .models import identifier
            identifier(args.id)
        with TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval) as adapter:
            if args.command == "inspect-series":
                result = adapter.inspect_series_metadata(args.id) if args.metadata else adapter.inspect_series(args.id,strict_order=False)
            else:
                result = adapter.inspect_library_filter(args.category,args.content_type) if args.content_type else adapter.inspect_library(details=True, category=args.category) if args.details else (
                    adapter.inspect_library() if args.category == "subscribed" else adapter.inspect_library(category=args.category))
            print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "archive-episode":
        if args.manual and (args.headless or args.http_only or any(value is None for value in (args.series_id, args.series_title, args.title, args.order))):
            raise ArchiveError("Manual capture requires all metadata options and a browser.")
        if args.page_selector and not args.manual:
            raise ArchiveError("Custom page selectors require --manual.")
        if args.http_only:
            from .adapters.tapas_http import TapasHTTPAdapter
            context = TapasHTTPAdapter(args.session_dir, interval=args.request_interval)
        else:
            from .adapters.tapas import TapasAdapter
            context = TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval)
        with context as adapter:
            from .network import bind_adapter
            bind_adapter(adapter,args.output)
            if args.manual:
                manifest = adapter.capture(args.url, args.series_id, args.series_title,
                                           args.title, args.order, args.page_selector)
            elif args.http_only:
                manifest = adapter.capture(args.url, args.series_id, args.series_title, args.title, args.order)
            else:
                manifest = adapter.capture_automatic(args.url, args.series_id, args.series_title, args.title, args.order)
            store = Store(args.output)
            try:
                print(store.archive(manifest, adapter, args.refresh, download_workers=args.download_workers))
            finally:
                store.close()
    else:
        if args.command == "archive-series" and not args.manifest:
            from .adapters.tapas import TapasAdapter
            from .importer import archive_series
            with TapasAdapter(args.session_dir, args.browser, args.headless, args.request_interval) as adapter:
                result = archive_series(adapter, args.id, args.output, args.limit, args.refresh)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if not args.manifest:
            raise ArchiveError("Complete account-library enumeration is unverified. Use archive-series for one series, or --manifest for local files.")
        adapter = LocalAdapter(args.manifest)
        manifest = adapter.manifest()
        if args.command == "archive-series" and manifest.series.id != args.id:
            raise ArchiveError("Requested series ID does not match the local manifest.")
        store = Store(args.output)
        try:
            print(store.archive(manifest, adapter, args.refresh))
        finally:
            store.close()
    return 0


def run_beside_engine(args):
    """The app's built-in engine needs the main thread for Qt, so with it the command runs on a second one."""
    if args.browser != 'qt':
        return run(args)
    from .qt_engine import serve
    return serve(lambda: run(args))


def main():
    args = parser().parse_args()
    try:
        code = run_beside_engine(args)
    except ArchiveError as e:
        print(f"Error: {e}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("Interrupted; completed pages remain cached for resume.", file=sys.stderr)
        code = 130
    except EOFError:
        print("Interactive browser step needs a terminal.", file=sys.stderr)
        code = 1
    except Exception:
        # Browser/http exceptions can include signed URLs or headers. Never echo them.
        print("Operation failed. Check browser installation, input files and output permissions. Raw exceptions are suppressed to protect session data.", file=sys.stderr)
        code = 1
    raise SystemExit(code)
