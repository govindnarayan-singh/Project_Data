from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from cli import native_postgres as runtime

READY = {'status': 'READY', 'version': '17.11', 'port': 5434, 'database': 'grdb'}


def test_tools_do_not_inherit_captured_pipes(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(runtime.subprocess, 'run', run)
    runtime._run_tool('pg_ctl.exe', ['start', '-W'])
    for stream in ('stdin', 'stdout', 'stderr'):
        assert run.call_args.kwargs[stream] == runtime.subprocess.DEVNULL
    assert run.call_args.kwargs['timeout'] == 3


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(runtime, '_validate_config', Mock())
    monkeypatch.setattr(runtime, '_startup_lock', lambda deadline: nullcontext())
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)


def test_healthy_is_cheap_and_never_restarts(monkeypatch, isolated):
    monkeypatch.setattr(runtime, '_probe', Mock(return_value=READY))
    tool = Mock(side_effect=AssertionError('No pg_ctl on healthy path'))
    monkeypatch.setattr(runtime, '_run_tool', tool)
    assert runtime.ensure_native_postgres_running()['started'] is False
    assert runtime.ensure_native_postgres_running()['started'] is False
    tool.assert_not_called()


def test_cold_start_then_rapid_second_call(monkeypatch, isolated):
    monkeypatch.setattr(runtime, '_probe', Mock(side_effect=[{'status': 'STOPPED'}, {'status': 'STOPPED'}, READY, READY]))
    monkeypatch.setattr(runtime, '_cluster_status', lambda: 3)
    tool = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(runtime, '_run_tool', tool)
    assert runtime.ensure_native_postgres_running()['started'] is True
    assert runtime.ensure_native_postgres_running()['started'] is False
    assert tool.call_count == 1
    name, command, timeout = tool.call_args.args
    assert name == 'pg_ctl.exe' and 'start' in command and '-W' in command
    assert '-h 127.0.0.1 -p 5434' in command
    assert str(runtime.settings.postgres_data) in command
    assert 0 < timeout <= 5


def test_another_caller_started_before_lock(monkeypatch, isolated):
    monkeypatch.setattr(runtime, '_probe', Mock(side_effect=[{'status': 'STOPPED'}, READY]))
    tool = Mock(side_effect=AssertionError('Must not start twice'))
    monkeypatch.setattr(runtime, '_run_tool', tool)
    assert not runtime.ensure_native_postgres_running()['started']


def test_concurrent_cold_calls_share_real_os_lock(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, local
    barrier, running, caller = Barrier(2), Event(), local()
    monkeypatch.setattr(runtime, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(runtime, '_validate_config', Mock())
    monkeypatch.setattr(runtime, '_cluster_status', lambda: 3)

    def probe(deadline):
        if not getattr(caller, 'checked', False):
            caller.checked = True
            barrier.wait(timeout=3)
            return {'status': 'STOPPED'}
        return READY if running.is_set() else {'status': 'STOPPED'}

    def start(*args):
        running.set()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime, '_probe', probe)
    tool = Mock(side_effect=start)
    monkeypatch.setattr(runtime, '_run_tool', tool)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.ensure_native_postgres_running(), range(2)))
    assert sum(result['started'] for result in results) == 1
    assert tool.call_count == 1


def test_starting_cluster_is_waited_not_restarted(monkeypatch, isolated):
    monkeypatch.setattr(runtime, '_probe', Mock(side_effect=[{'status': 'STARTING'}, {'status': 'STARTING'}, READY]))
    monkeypatch.setattr(runtime, '_run_tool', Mock(side_effect=AssertionError('No start')))
    assert not runtime.ensure_native_postgres_running()['started']


def fake_connection(monkeypatch, row):
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = row
    connect = Mock(return_value=nullcontext(connection))
    monkeypatch.setattr(runtime.psycopg, 'connect', connect)
    return connect


def test_live_identity_connection_is_readonly_and_pinned(monkeypatch):
    connect = fake_connection(monkeypatch, (170011, '17.11', 'grdb', 5434, str(runtime.settings.postgres_data)))
    assert runtime._verify_identity()['data_directory_verification'] == 'SQL_VERIFIED'
    assert connect.call_args.kwargs['port'] == 5434
    assert connect.call_args.kwargs['hostaddr'] == '127.0.0.1'
    assert 'default_transaction_read_only=on' in connect.call_args.kwargs['options']


@pytest.mark.parametrize('row', [
    (130000, '13', 'grdb', 5434, None),
    (170011, '17.11', 'wrong', 5434, None),
    (170011, '17.11', 'grdb', 5432, None),
    (170011, '17.11', 'grdb', 5434, 'D:/wrong-cluster'),
])
def test_wrong_identity_fails_closed(monkeypatch, row):
    fake_connection(monkeypatch, row)
    with pytest.raises(runtime.NativePostgresError, match='Wrong'):
        runtime._verify_identity()


def test_unprivileged_setting_uses_expected_cluster_status(monkeypatch):
    fake_connection(monkeypatch, (170011, '17.11', 'grdb', 5434, None))
    monkeypatch.setattr(runtime, '_cluster_status', lambda: 0)
    assert runtime._verify_identity()['data_directory_verification'].startswith('PG_CTL')
    monkeypatch.setattr(runtime, '_cluster_status', lambda: 3)
    with pytest.raises(runtime.NativePostgresError):
        runtime._verify_identity()


def test_login_failure_does_not_expose_connection_password(monkeypatch):
    monkeypatch.setattr(runtime.psycopg, 'connect', Mock(side_effect=psycopg.OperationalError('secret-password')))
    with pytest.raises(runtime.NativePostgresError) as exc:
        runtime._verify_identity()
    assert 'secret-password' not in str(exc.value)
    assert 'login' in str(exc.value)


@pytest.mark.parametrize('url', [
    'postgresql://user:secret@127.0.0.1:5432/grdb',
    'postgresql://user:secret@127.0.0.1:5434/other',
    'postgresql://user:secret@remote:5434/grdb',
    'postgresql://user:secret@127.0.0.1:5434/grdb?hostaddr=127.0.0.2',
])
def test_unsafe_config_rejected_before_tools(monkeypatch, url):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, database_url=url))
    with pytest.raises(runtime.NativePostgresError) as exc:
        runtime._validate_config()
    assert 'secret' not in str(exc.value)


def test_missing_or_wrong_cluster_never_initialized(monkeypatch, tmp_path):
    binary, data, logs = [tmp_path / name for name in ('bin', 'data', 'logs')]
    for path in (binary, data, logs):
        path.mkdir()
    for name in ('pg_ctl.exe', 'pg_isready.exe'):
        (binary / name).touch()
    monkeypatch.setattr(runtime, 'EXPECTED_BIN', binary)
    monkeypatch.setattr(runtime, 'EXPECTED_DATA', data)
    monkeypatch.setattr(runtime, 'EXPECTED_LOGS', logs)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, postgres_bin=binary, postgres_data=data, postgres_logs=logs))
    with pytest.raises(runtime.NativePostgresError, match='initialization is forbidden'):
        runtime._validate_config()
    (data / 'PG_VERSION').write_text('13')
    with pytest.raises(runtime.NativePostgresError, match='not PostgreSQL 17'):
        runtime._validate_config()
    (data / 'PG_VERSION').write_text('17')
    runtime._validate_config()


def test_unresponsive_port_is_not_mistaken_for_stopped(monkeypatch):
    monkeypatch.setattr(runtime, '_run_tool', Mock(return_value=SimpleNamespace(returncode=2)))
    socket = Mock()
    socket.bind.side_effect = OSError('Address in use')
    monkeypatch.setattr(runtime.socket, 'socket', Mock(return_value=nullcontext(socket)))
    with pytest.raises(runtime.NativePostgresError, match='occupied'):
        runtime._probe(runtime.time.monotonic() + 5)


def test_start_failure_has_only_bounded_redacted_tail(monkeypatch, isolated, tmp_path):
    monkeypatch.setattr(runtime, '_probe', Mock(return_value={'status': 'STOPPED'}))
    monkeypatch.setattr(runtime, '_cluster_status', lambda: 3)
    monkeypatch.setattr(runtime, '_run_tool', Mock(return_value=SimpleNamespace(returncode=1)))
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, postgres_logs=tmp_path,
        database_url='postgresql://user:verysecret@127.0.0.1:5434/grdb'))
    (tmp_path / 'postgresql.log').write_text('\n'.join(f'{i} password=verysecret' for i in range(1000)))
    with pytest.raises(runtime.NativePostgresError) as exc:
        runtime.ensure_native_postgres_running()
    assert 'verysecret' not in str(exc.value)
    assert len(str(exc.value).splitlines()) <= 10


def test_timeout_is_bounded(monkeypatch, isolated):
    monkeypatch.setattr(runtime, '_probe', Mock(return_value={'status': 'STARTING'}))
    monkeypatch.setattr(runtime, '_remaining', Mock(side_effect=runtime.NativePostgresError('Timed out')))
    monkeypatch.setattr(runtime, '_log_tail', lambda: '')
    with pytest.raises(runtime.NativePostgresError, match='Timed out'):
        runtime.ensure_native_postgres_running()


@pytest.mark.parametrize('value', [0, 31, 120])
def test_timeout_limits(value):
    with pytest.raises(runtime.NativePostgresError, match='between 5 and 30'):
        runtime.ensure_native_postgres_running(value)


@pytest.mark.parametrize('flag,debug,ci,expected', [
    (None, 'true', '', True), (None, 'false', '', False), (None, 'true', 'true', False),
    ('false', 'true', '', False), ('true', 'false', 'true', True), ('off', 'true', '', False),
])
def test_autostart_flags(monkeypatch, flag, debug, ci, expected):
    monkeypatch.delenv('AUTO_START_POSTGRES', raising=False)
    if flag is not None:
        monkeypatch.setenv('AUTO_START_POSTGRES', flag)
    monkeypatch.setenv('DJANGO_DEBUG', debug)
    monkeypatch.setenv('CI', ci)
    assert runtime.auto_start_enabled() is expected


@pytest.mark.parametrize('command,extra,enabled,ensures', [
    ('runserver', [], True, 1), ('runserver', ['--noreload'], True, 1),
    ('runserver', ['--help'], True, 0), ('runserver', [], False, 0),
    ('check', [], True, 0), ('migrate', [], True, 0), ('test', [], True, 0),
])
def test_manage_only_runserver_ensures(monkeypatch, command, extra, enabled, ensures):
    import manage
    import django.core.management
    monkeypatch.setattr(manage.sys, 'argv', ['manage.py', command, *extra])
    monkeypatch.setattr(runtime, 'auto_start_enabled', lambda: enabled)
    ensure = Mock(return_value=READY)
    monkeypatch.setattr(runtime, 'ensure_native_postgres_running', ensure)
    execute = Mock()
    monkeypatch.setattr(django.core.management, 'execute_from_command_line', execute)
    manage.main()
    assert ensure.call_count == ensures
    execute.assert_called_once()


def test_manage_fails_before_django_on_wrong_server(monkeypatch):
    import manage
    import django.core.management
    monkeypatch.setattr(manage.sys, 'argv', ['manage.py', 'runserver'])
    monkeypatch.setattr(runtime, 'auto_start_enabled', lambda: True)
    monkeypatch.setattr(runtime, 'ensure_native_postgres_running', Mock(side_effect=runtime.NativePostgresError('Wrong server')))
    execute = Mock()
    monkeypatch.setattr(django.core.management, 'execute_from_command_line', execute)
    with pytest.raises(SystemExit) as exc:
        manage.main()
    assert exc.value.code == 1
    execute.assert_not_called()


def test_preflight_accepts_parent_git_and_rejects_external_write(monkeypatch):
    from cli import dev_preflight
    monkeypatch.setattr(dev_preflight.subprocess, 'run', Mock(return_value=SimpleNamespace(stdout='D:/LLM_Project_data\n')))
    assert dev_preflight.workspace_preflight(['logs/proposed.txt'])['workspace_guard_status'] == 'PASS'
    with pytest.raises(runtime.NativePostgresError, match='Workspace guard'):
        dev_preflight.workspace_preflight(['../PostgreSQL/data17/project-file.txt'])
