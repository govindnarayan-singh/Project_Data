"""Single runtime authority for the existing native PostgreSQL 17 cluster.

No initialization, migration, credentials in subprocess arguments, or fallback DB.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict

from knowledge_engine.settings import PROJECT_ROOT, settings

NATIVE_ROOT = Path('D:/LLM_Project_data/PostgreSQL')
EXPECTED_BIN = NATIVE_ROOT / '17/pgsql/bin'
EXPECTED_DATA = NATIVE_ROOT / 'data17'
EXPECTED_LOGS = NATIVE_ROOT / 'logs'
READY_MESSAGE = 'PostgreSQL 17 ready at 127.0.0.1:5434/grdb'


class NativePostgresError(RuntimeError):
    """Safe, credential-free diagnostic for local runtime failures."""


def _validate_config() -> None:
    try:
        params = conninfo_to_dict(settings.database_url)
    except psycopg.Error:
        raise NativePostgresError('Invalid DATABASE_URL; expected 127.0.0.1:5434/grdb') from None
    if (params.get('host'), params.get('port'), params.get('dbname')) != ('127.0.0.1', '5434', 'grdb'):
        raise NativePostgresError('Native runtime requires DATABASE_URL at 127.0.0.1:5434/grdb; no fallback')
    if params.get('hostaddr', '127.0.0.1') != '127.0.0.1' or params.get('service'):
        raise NativePostgresError('DATABASE_URL hostaddr/service cannot redirect the native runtime')
    for actual, expected in ((settings.postgres_bin, EXPECTED_BIN), (settings.postgres_data, EXPECTED_DATA),
                             (settings.postgres_logs, EXPECTED_LOGS)):
        if actual.resolve() != expected.resolve():
            raise NativePostgresError(f'Native runtime path must be {expected}')
    for name in ('pg_ctl.exe', 'pg_isready.exe'):
        if not (settings.postgres_bin / name).is_file():
            raise NativePostgresError(f'Missing existing native tool: {name}; no installation attempted')
    try:
        version = (settings.postgres_data / 'PG_VERSION').read_text().strip()
    except OSError:
        raise NativePostgresError('Existing data17 cluster not accessible; initialization is forbidden') from None
    if version != '17':
        raise NativePostgresError('Existing cluster is not PostgreSQL 17; refusing operation')
    if not settings.postgres_logs.is_dir():
        raise NativePostgresError('Existing PostgreSQL logs directory is missing')


def _run_tool(name: str, args: list[str], timeout: float = 3) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(settings.postgres_bin / name), *args],
                              # A spawned Windows server can inherit PIPE handles,
                              # making communicate() wait forever after pg_ctl exits.
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False,
                              timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise NativePostgresError(f'{name} failed or exceeded its bounded timeout') from None


def _remaining(deadline: float, limit: float = 3) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NativePostgresError('Timed out waiting for native PostgreSQL 17')
    return min(remaining, limit)


def _verify_identity() -> dict:
    try:
        with psycopg.connect(settings.database_url, host='127.0.0.1', hostaddr='127.0.0.1',
                             port=5434, dbname='grdb', connect_timeout=2, autocommit=True,
                             options='-c default_transaction_read_only=on -c statement_timeout=1500') as connection:
            row = connection.execute("""SELECT current_setting('server_version_num')::int,
                current_setting('server_version'), current_database(), inet_server_port(),
                (SELECT setting FROM pg_settings WHERE name='data_directory')""").fetchone()
    except psycopg.Error:
        raise NativePostgresError('Port 5434 responds but application identity/login could not be verified; check credentials and server logs') from None
    version_num, version, database, port, data_directory = row
    if not (170000 <= version_num < 180000) or database != 'grdb' or port != 5434:
        raise NativePostgresError('Wrong server identity on port 5434; expected PostgreSQL 17 / grdb; no process was stopped')
    if data_directory:
        if Path(data_directory).resolve() != settings.postgres_data.resolve():
            raise NativePostgresError('Wrong data_directory on port 5434; no process was stopped')
        directory_check = 'SQL_VERIFIED'
    else:
        # Unprivileged roles can see NULL for this setting. Never grant privileges.
        if _cluster_status() != 0:
            raise NativePostgresError('data_directory unavailable and expected cluster is not running')
        directory_check = 'PG_CTL_VERIFIED_SQL_SETTING_NOT_VISIBLE'
    return {'status': 'READY', 'host': '127.0.0.1', 'port': port, 'database': database,
            'version': version, 'data_directory': str(settings.postgres_data),
            'data_directory_verification': directory_check}


def _cluster_status() -> int:
    result = _run_tool('pg_ctl.exe', ['-D', str(settings.postgres_data), 'status'])
    if result.returncode not in (0, 3):
        raise NativePostgresError('Cannot inspect existing cluster status; check data17 permissions')
    return result.returncode


def _probe(deadline: float) -> dict:
    user = conninfo_to_dict(settings.database_url).get('user')
    user_args = ['-U', user] if user else []
    result = _run_tool('pg_isready.exe', ['-h', '127.0.0.1', '-p', '5434', '-d', 'grdb', '-t', '1', *user_args],
                       _remaining(deadline))
    if result.returncode == 0:
        _remaining(deadline)
        return _verify_identity()
    if result.returncode == 1:
        return {'status': 'STARTING'}
    if result.returncode != 2:
        raise NativePostgresError('Native readiness check failed')
    try:
        # Binding (without listening) distinguishes a free port from a silent
        # non-PostgreSQL listener; a failed TCP connect alone cannot do this.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', 5434))
    except OSError:
        raise NativePostgresError('Port 5434 is occupied or unavailable, not a ready PostgreSQL server; no process was stopped') from None
    return {'status': 'STOPPED'}


def native_postgres_status() -> dict:
    """Read-only readiness and identity check; never starts the cluster."""
    _validate_config()
    return _probe(time.monotonic() + 6)


@contextmanager
def _startup_lock(deadline: float):
    # OS-held lock releases on crash; never delete stale PID/lock files in data17.
    path = PROJECT_ROOT / 'logs/native_postgres.lock'
    if not path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        raise NativePostgresError('Runtime lock must remain inside the application')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        while True:
            _remaining(deadline)
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(min(.1, _remaining(deadline)))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _log_tail() -> str:
    try:
        with (settings.postgres_logs / 'postgresql.log').open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 4096))
            text = '\n'.join(handle.read().decode('utf-8', errors='replace').splitlines()[-8:])[-2000:]
        password = conninfo_to_dict(settings.database_url).get('password')
        if password:
            text = text.replace(password, '[REDACTED]')
        text = re.sub(r'postgres(?:ql)?://\S+', '[REDACTED_DATABASE_URL]', text, flags=re.I)
        return '\n'.join('[REDACTED credential-related log line]' if re.search(r'password', line, re.I)
                         else line for line in text.splitlines())
    except (OSError, psycopg.Error):
        return '(startup log unavailable)'


def ensure_native_postgres_running(timeout: float = 30) -> dict:
    """Idempotent ensure, shared by CLI and runserver; no restart on success."""
    if not 5 <= timeout <= 30:
        raise NativePostgresError('Startup timeout must be between 5 and 30 seconds')
    _validate_config()
    deadline = time.monotonic() + timeout
    state = _probe(deadline)
    if state['status'] == 'READY':
        return {**state, 'started': False}
    try:
        with _startup_lock(deadline):
            state = _probe(deadline)
            started = False
            if state['status'] == 'STOPPED' and _cluster_status() == 3:
                # Do not reuse postmaster.opts; force the intended loopback endpoint.
                result = _run_tool('pg_ctl.exe', ['-D', str(settings.postgres_data),
                    '-l', str(settings.postgres_logs / 'postgresql.log'), '-o', '-h 127.0.0.1 -p 5434',
                    'start', '-W'], _remaining(deadline, 5))
                started = result.returncode == 0
                if not started and _cluster_status() != 0:
                    raise NativePostgresError('Could not start the existing PostgreSQL 17 cluster')
            while state['status'] != 'READY':
                time.sleep(min(.2, _remaining(deadline)))
                state = _probe(deadline)
            return {**state, 'started': started}
    except NativePostgresError as exc:
        raise NativePostgresError(f'{exc}\nStartup log (last eight lines at most):\n{_log_tail()}') from None
    except OSError:
        raise NativePostgresError('Cannot access the application startup lock or existing runtime directories') from None


def stop_native_postgres() -> dict:
    """Explicit maintenance only; runserver never calls this."""
    state = native_postgres_status()
    if state['status'] == 'STOPPED' and _cluster_status() == 3:
        return {'status': 'STOPPED'}
    if state['status'] != 'READY':
        raise NativePostgresError('Refusing stop until the expected server identity can be verified')
    result = _run_tool('pg_ctl.exe', ['-D', str(settings.postgres_data), 'stop', '-m', 'fast', '-w', '-t', '25'], 30)
    if result.returncode:
        raise NativePostgresError('Native PostgreSQL stop failed')
    return {'status': 'STOPPED'}


def auto_start_enabled() -> bool:
    """dotenv is already loaded by project settings; explicit env takes priority."""
    raw = os.getenv('AUTO_START_POSTGRES')
    if raw is None:
        return os.getenv('DJANGO_DEBUG', 'true').strip().lower() in {'1', 'true', 'yes', 'on'} and os.getenv('CI', '').strip().lower() in {'', '0', 'false', 'no', 'off'}
    if raw.strip().lower() not in {'1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'}:
        raise NativePostgresError('AUTO_START_POSTGRES must be true or false')
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def main() -> None:
    parser = argparse.ArgumentParser(description='Manage only the existing native PostgreSQL 17 cluster')
    parser.add_argument('action', choices=('status', 'ensure', 'start', 'stop'))
    args = parser.parse_args()
    try:
        if args.action in {'ensure', 'start'}:
            ensure_native_postgres_running()
            print(READY_MESSAGE)
        elif args.action == 'stop':
            stop_native_postgres()
            print('Native PostgreSQL 17 stopped')
        else:
            state = native_postgres_status()
            if state['status'] != 'READY':
                raise NativePostgresError(f"Native PostgreSQL 17 {state['status'].lower()}; run python -m cli.native_postgres ensure")
            print(READY_MESSAGE)
    except NativePostgresError as exc:
        print(f'PostgreSQL: {exc}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
