"""Opt-in independent connected host. Importing this module starts nothing."""

import argparse
import asyncio
import json
import os
import shlex
import signal
import sys
from pathlib import Path

RETAINED = []  # Actual owners survive unknown terminal cleanup; never fake join.


def parser():
    result = argparse.ArgumentParser(
        prog="python -m comic_archive.connected",
        description="Read-only online comic library; independent of downloader accounts and workers.",
    )
    commands = result.add_subparsers(dest="command", required=True)
    index = commands.add_parser(
        "index", help="Explicitly register selected completed CBZ files"
    )
    index.add_argument("--root", type=Path, required=True)
    index.add_argument("--index", type=Path, required=True)
    index.add_argument("--series", required=True)
    index.add_argument(
        "books", nargs="+", help="Selected relative CBZ paths under that root"
    )
    serve = commands.add_parser(
        "serve", help="Keep approved online libraries available"
    )
    serve.add_argument("--state", type=Path, required=True)
    serve.add_argument(
        "--origin",
        required=True,
        help="Reader HTTPS origin; loopback HTTP only for tests",
    )
    serve.add_argument(
        "--library",
        nargs=2,
        action="append",
        required=True,
        metavar=("ROOT", "PRIVATE_INDEX"),
    )
    serve.add_argument("--ice-config", type=Path, help="Owned private JSON connection settings (one STUN and one TURN server)")
    serve.add_argument(
        "--init",
        action="store_true",
        help="Explicitly initialize a NEW private state directory",
    )
    serve.add_argument(
        "--interactive",
        action="store_true",
        help="Local TTY pairing and approval commands",
    )
    serve.add_argument(
        "--pairing",
        action="append",
        help="Serve only these existing approved pairing UUIDs",
    )
    return result


def emit(value):
    # Safe status fields only. ASCII JSON also escapes terminal control chars in
    # an account label. No automatic pairing link/key/token/private path output.
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)


def register(args):
    from .files import Index, SharedRoot

    root = SharedRoot(args.root)
    index = None
    try:
        index = Index(args.index, root)
        for book in args.books:
            record = index.register_completed(book, args.series)
            emit(
                dict(
                    share_id=index.share_id,
                    book_id=record["book_id"],
                    content_version=record["content_version"],
                )
            )
    finally:
        if index is not None:
            index.close()
        root.close()


def pending(controller, pairing_id):
    from .jsoncodec import require

    found = next(
        (item for item in controller.pending() if item.draft.pairing_id == pairing_id),
        None,
    )
    require(found is not None, "not_found")
    return found  # The actual retained local pending object, never reconstructed.


async def reviews(controller):
    reported = set()
    while not controller.closed:
        controller.owner.expire()
        current = set()
        for item in controller.pending():
            key = (item.draft.pairing_id, item.phase)
            current.add(key)
            if item.phase == "review" and key not in reported:
                review = item.review
                controller.owner.describe(review)
                emit(
                    dict(
                        event="approval_required",
                        pairing_id=item.draft.pairing_id,
                        account=item.account_label,
                        account_binding=review.account_binding,
                        reader_device_id=review.reader_keys["device_id"],
                        shares=list(controller.owner._indices),
                    )
                )
        reported = current
        await asyncio.sleep(0.25)


async def console(driver, terminal, shutdown):
    from .files import SourceError
    from .jsoncodec import require

    controller = driver.controller
    emit(
        dict(
            commands=[
                "pair",
                "link PAIR",
                "resume PAIR",
                "approve PAIR SHARE...",
                "status",
                "start PAIR",
                "stop PAIR",
                "unshare SHARE",
                "revoke PAIR",
                "retry-revoke PAIR",
                "quit",
            ]
        )
    )
    while not shutdown.is_set():
        line = await terminal.readline()
        if line is None:
            shutdown.set()
            return
        try:
            words = shlex.split(line)
            if not words:
                continue
            command, *args = words
            if command == "quit" and not args:
                shutdown.set()
            elif command == "pair" and not args:
                require(
                    not controller.pending(), "busy"
                )  # Resume uncertain work, no duplicate pair.
                item = await controller.begin()
                emit(dict(pairing_id=item.draft.pairing_id, phase=item.phase))
            elif command == "link" and len(args) == 1:
                # Secret is displayed ONLY after explicit local command, on the
                # verified native TTY. Never in routine stdout/service logs.
                require(os.isatty(sys.stdout.fileno()), "unsupported_format")
                print(controller.qr_url(pending(controller, args[0])), flush=True)
            elif command == "resume" and len(args) == 1:
                await controller.resume(pending(controller, args[0]))
            elif command == "approve" and 2 <= len(args) <= 17:
                item = pending(controller, args[0])
                require(item.phase == "review", "revoked")
                selected = [controller.owner._indices[share] for share in args[1:]]
                emit(await driver.approve(item, item.review, selected))
            elif command == "status" and not args:
                emit(
                    dict(
                        connections=driver.status(),
                        pending=[
                            dict(pairing_id=p.draft.pairing_id, phase=p.phase)
                            for p in controller.pending()
                        ],
                        shares=list(controller.owner._indices),
                    )
                )
            elif command == "start" and len(args) == 1:
                driver.enable(args[0])
            elif command == "stop" and len(args) == 1:
                await driver.stop(args[0])
            elif command == "unshare" and len(args) == 1:
                emit(dict(affected=await driver.withdraw_share(args[0])))
            elif command == "revoke" and len(args) == 1:
                await driver.revoke(args[0])
            elif command == "retry-revoke" and len(args) == 1:
                await controller.reconcile_revoked(args[0])
            else:
                emit(dict(error="invalid_command"))
        except SourceError as error:
            emit(dict(error=error.code))
        except (KeyError, ValueError):
            emit(dict(error="invalid_command"))


async def serve(args):
    from .controller import HostController
    from .driver import HostDriver
    from .ice import read_servers
    from .files import Index, SharedRoot
    from .jsoncodec import require
    from .pairing import PairingOwner
    from .state import HostState
    from .terminal import TerminalInput

    require(os.name == "posix" and 1 <= len(args.library) <= 16, "unsupported_format")
    ice_servers = read_servers(args.ice_config) if args.ice_config is not None else []
    if args.interactive:
        require(
            os.isatty(sys.stdin.fileno()) and os.isatty(sys.stdout.fileno()),
            "unsupported_format",
        )
    roots, opened_indices, indices = [], [], {}
    state, owner, controller, driver, terminal = None, None, None, None, None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed, tasks = [], set()
    try:
        for source, private in args.library:
            require(
                Path(private).is_file(), "not_found"
            )  # Serve never silently creates a replacement index.
            root = SharedRoot(Path(source))
            roots.append(root)
            index = Index(Path(private), root)
            opened_indices.append(index)
            # Retain each actual opened handle before checking duplicate IDs.
            if index.share_id in indices:
                require(False, "integrity_failed")
            indices[index.share_id] = index
        state = HostState.open(args.state, roots=roots, create=args.init)
        owner = PairingOwner(state, indices)
        controller = HostController(owner, args.origin)
        driver = HostDriver.start(controller, ice_servers=ice_servers)
        approved = [
            row["pairing_id"] for row in state.records() if row["status"] == "approved"
        ]
        selected = args.pairing if args.pairing is not None else approved
        require(
            len(selected) <= 16 and len(set(selected)) == len(selected),
            "resource_limit",
        )
        for pair in selected:
            driver.enable(pair)
        for number in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(number, stopping.set)
            installed.append(number)
        emit(dict(status="starting", pairings=len(selected), shares=list(indices)))
        if args.interactive:
            terminal = TerminalInput.open(sys.stdin.fileno())
            tasks = {
                asyncio.create_task(console(driver, terminal, stopping)),
                asyncio.create_task(reviews(controller)),
            }
        waiter = asyncio.create_task(stopping.wait())
        tasks.add(waiter)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stopping.set()
        if driver is not None:
            driver.closed = True
            for pair in driver._slots:
                driver.pause(
                    pair
                )  # Permission retirement precedes even terminal cleanup.
        terminal_error = None
        if terminal is not None:
            try:
                terminal.close()
            except BaseException as error:
                terminal_error = error
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if driver is not None:
                await driver.close()
            elif controller is not None:
                await controller.close()
        except BaseException:
            RETAINED.append(
                (
                    state,
                    owner,
                    controller,
                    driver,
                    opened_indices,
                    roots,
                    terminal,
                    tasks,
                )
            )
            raise  # Never retire selected sources while actual joins are unknown.
        if owner is not None:
            owner.close()
        elif state is not None:
            state.close()
        for index in opened_indices:
            index.close()
        for root in roots:
            root.close()
        for number in installed:
            loop.remove_signal_handler(number)
        if terminal_error is not None:
            RETAINED.append(terminal)
            raise terminal_error


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "index":
            register(args)
        else:
            asyncio.run(serve(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Dependencies, filesystem and network exceptions can contain paths or
        # private protocol material. Do not print raw exceptions/tracebacks.
        print(
            "Connected host unavailable; check optional dependencies, selected files and private state. Retained cleanup may require attention.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
