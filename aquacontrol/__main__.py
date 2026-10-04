"""Entry point: `python3 -m aquacontrol <command>`."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

from .auth import hash_password, hash_token, new_token
from .backups import BackupStore
from .climate import ClimateController, make_client_factory
from .config import AppConfig, ConfigError, load_daemon_config, update_daemon_config
from .device import Device
from .monitor import Monitor
from .schedule import Scheduler
from .sensors import ExternalStore, read_host_sensors
from .transport import HidrawReader, HidrawTransport
from .validate import make_check
from .web import App, make_server

log = logging.getLogger("aquacontrol")
DEFAULT_DAEMON = "/etc/aquacontrol/daemon.json"
DEFAULT_APP = "/var/lib/aquacontrol/config.json"
SHUTDOWN_JOIN_S = 90  # systemd's default stop timeout; a switch-on talks to Home Assistant up to 8 times at 10 s each
STATIC = Path(__file__).resolve().parent.parent / "static"


SHUTDOWN_MONITOR_JOIN_S = 10  # the monitor loop wakes up at least every second
HISTORY_FILE = "history.json.gz"


def history_path(args) -> Path:
    """The saved history lives next to config.json (so --fake dev runs write into dev/)."""
    return Path(args.app_config).parent / HISTORY_FILE


def build(args) -> tuple[Device, Monitor, Scheduler, BackupStore, AppConfig, ExternalStore]:
    config = AppConfig(args.app_config)
    backups = BackupStore(config.backup_dir, config.backup_keep)
    if args.fake:
        from .fake import FakeReader, FakeTransport
        fixtures = Path(args.fake)
        transport = FakeTransport((fixtures / "settings_live.bin").read_bytes(),
                                  (fixtures / "names.bin").read_bytes())
        open_reader = lambda: FakeReader((fixtures / "status.bin").read_bytes())  # noqa: E731
    else:
        transport = HidrawTransport()
        open_reader = HidrawReader
    device = Device(transport, backups, make_check(config.min_percent()))
    try:
        externals = ExternalStore()
        monitor = Monitor(open_reader, extra=lambda: read_host_sensors(config.host_sensor_labels()) + externals.current(),
                          history_path=history_path(args))
        scheduler = Scheduler(device, config.rules)
    except BaseException:
        device.close()  # the device exists: do not leave it open
        raise
    return device, monitor, scheduler, backups, config, externals


def build_climate(monitor: Monitor, config: AppConfig) -> tuple[ClimateController, Callable[[], object | None]]:
    """The climate controller (idle while disabled in the config) and the HA client factory it shares with the API."""
    factory = make_client_factory(config)
    return ClimateController(monitor.snapshot, factory, config.climate_config), factory


def cmd_run(args) -> int:
    try:
        daemon = load_daemon_config(args.daemon_config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if not daemon.password_hash:
        log.error("kein Passwort gesetzt: python3 -m aquacontrol set-password")
        return 2
    # Handlers first: a signal during startup (device open, TLS load, socket bind) must lead to a
    # clean shutdown too, not to the default action. The handler only sets the stop event, from a
    # helper thread, because Event.set() takes a lock the interrupted main thread might hold.
    stop = threading.Event()

    def request_stop(*_):
        threading.Thread(target=stop.set, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    device, monitor, scheduler, backups, config, externals = build(args)
    # From here on the device exists: every way out of this function closes it (see `finally`).
    threads: list[threading.Thread] = []
    server = serving = None
    history_loaded = False  # never overwrite the saved history with an empty one if startup failed before the load
    failure: list[BaseException] = []
    try:
        climate, ha_client_factory = build_climate(monitor, config)
        app = App(device, monitor, scheduler, backups, config, externals, daemon.password_hash,
                  daemon.push_tokens, args.static, climate=climate, ha_client_factory=ha_client_factory)
        creds = os.environ.get("CREDENTIALS_DIRECTORY")
        cert = key = None
        if creds and not args.no_tls:
            cert, key = os.path.join(creds, "cert"), os.path.join(creds, "key")
        elif not args.no_tls:
            log.error("kein Zertifikat (CREDENTIALS_DIRECTORY fehlt); nur mit --no-tls lokal testen")
            return 2
        host = args.listen or daemon.listen
        port = args.port or daemon.port
        server = make_server(app, host, port, cert, key)
        monitor.load_history(history_path(args))  # before the monitor thread starts
        history_loaded = True
        threads.append(threading.Thread(target=monitor.run, args=(stop,), name="monitor", daemon=True))
        if not args.no_schedule:
            threads.append(threading.Thread(target=scheduler.run, args=(stop,), name="scheduler", daemon=True))
        threads.append(threading.Thread(target=climate.run, args=(stop,), name="climate", daemon=True))

        def serve():
            # A dead HTTP thread must not leave a daemon without an API running: stop everything and
            # exit non-zero so systemd restarts the service.
            try:
                server.serve_forever()
            except BaseException as e:
                log.exception("HTTP-Server abgestürzt, Dienst wird beendet")
                failure.append(e)
                stop.set()

        serving = threading.Thread(target=serve, name="http", daemon=True)
        for t in threads:
            t.start()
        serving.start()
        log.info("listening on %s://%s:%d", "https" if cert else "http", host, port)
        stop.wait()  # a signal that came earlier has already set it
    finally:
        # order: stop event -> http server -> scheduler and climate -> device -> socket. A running device
        # write (scheduler or request thread) finishes before the process exits; the climate thread only
        # talks to Home Assistant, but is joined too so no switch is cut off half way.
        stop.set()
        if serving is not None and serving.is_alive() and not failure:  # shutdown() waits for a live serve loop
            server.shutdown()
        if serving is not None and serving.ident is not None:
            serving.join(15)
        for t in threads:
            if t.name in ("scheduler", "climate") and t.ident is not None:
                t.join(SHUTDOWN_JOIN_S)
        for t in threads:
            if t.name == "monitor" and t.ident is not None:
                t.join(SHUTDOWN_MONITOR_JOIN_S)
        if history_loaded:
            try:
                monitor.save_history(history_path(args))
            except Exception as e:  # a full disk must not keep the device open
                log.warning("Verlauf konnte nicht gespeichert werden: %s", e)
        device.close()
        if server is not None:
            server.server_close()
    return 1 if failure else 0


def cmd_set_password(args) -> int:
    pw = getpass.getpass("Neues Passwort: ")
    if len(pw) < 10:
        print("Mindestens 10 Zeichen.", file=sys.stderr)
        return 1
    if getpass.getpass("Wiederholen: ") != pw:
        print("Stimmt nicht überein.", file=sys.stderr)
        return 1
    update_daemon_config(args.daemon_config, password_hash=hash_password(pw))
    print(f"Passwort in {args.daemon_config} gesetzt. Dienst neu starten: systemctl restart aquacontrol")
    return 0


def cmd_add_push_token(args) -> int:
    token = new_token()
    update_daemon_config(args.daemon_config, push_token=(args.source, hash_token(token)))
    print(token)
    print(f"Token für Quelle '{args.source}' gespeichert (nur der Hash). Dienst neu starten.", file=sys.stderr)
    return 0


def cmd_backup(args) -> int:
    config = AppConfig(args.app_config)
    store = BackupStore(config.backup_dir, config.backup_keep)
    device = Device(HidrawTransport(), store, make_check({}))
    name = store.save(device.read_report(), args.reason, pinned=args.pinned)
    print(name)
    return 0


def cmd_selftest_write(args) -> int:
    config = AppConfig(args.app_config)
    store = BackupStore(config.backup_dir, config.backup_keep)
    device = Device(HidrawTransport(), store, make_check({}))
    print("Backup:", store.save(device.read_report(), "selftest", pinned=True))
    device.rewrite_current()
    print("Unverändertes Profil geschrieben, bestätigt und Byte für Byte verifiziert.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aquacontrol")
    parser.add_argument("--daemon-config", default=DEFAULT_DAEMON)
    parser.add_argument("--app-config", default=DEFAULT_APP)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Daemon starten")
    run.add_argument("--static", default=str(STATIC))
    run.add_argument("--listen")
    run.add_argument("--port", type=int)
    run.add_argument("--no-tls", action="store_true", help="nur für lokale Entwicklung")
    run.add_argument("--no-schedule", action="store_true")
    run.add_argument("--fake", metavar="FIXTURE_DIR", help="simuliertes Gerät aus Fixture-Dateien")
    run.set_defaults(func=cmd_run)
    sub.add_parser("set-password").set_defaults(func=cmd_set_password)
    tok = sub.add_parser("add-push-token")
    tok.add_argument("source")
    tok.set_defaults(func=cmd_add_push_token)
    bak = sub.add_parser("backup")
    bak.add_argument("--reason", default="manual")
    bak.add_argument("--pinned", action="store_true")
    bak.set_defaults(func=cmd_backup)
    sub.add_parser("selftest-write").set_defaults(func=cmd_selftest_write)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
