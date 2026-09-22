"""Application-confined V3 artifact IO; no writes to prior experiments."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import sys

from knowledge_engine.settings import PROJECT_ROOT

ROOT = PROJECT_ROOT.resolve()
OUT = ROOT / 'outputs/graph_retrieval_v3'
TMP = ROOT / 'tmp/graph_retrieval_v3'
MANUAL = ROOT / 'data/retrieval_eval_graph_v3_manual_review.csv'


def guarded(path: Path | str) -> Path:
    path = Path(path)
    resolved = (path if path.is_absolute() else ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f'V3 write outside application: {resolved}')
    return resolved


def artifact_path(path: Path | str) -> Path:
    path = guarded(path)
    if not (path.is_relative_to(OUT) or path.is_relative_to(TMP) or path == MANUAL):
        raise ValueError(f'Not a V3 artifact destination: {path}')
    return path


def write_json(path: Path, value) -> None:
    path = artifact_path(path)
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def write_csv(path: Path, rows: list[dict], fields=None) -> None:
    path = artifact_path(path)
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(fields or (rows[0].keys() if rows else ['status']))
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='raise')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v for k,v in row.items()})


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_files() -> dict:
    folders = ('knowledge_engine', 'cli', 'tests', 'config', 'apps', 'templates', 'static', 'docs', 'data',
               'outputs/graph_retrieval_v1', 'outputs/graph_retrieval_v2', 'outputs/recent_gr_holdout_v1')
    files = [ROOT / 'AGENTS.md', ROOT / 'manage.py', ROOT / '.env', ROOT / '.env.example']
    for folder in folders:
        files.extend((ROOT / folder).rglob('*'))
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in files if p.is_file()
            and '__pycache__' not in p.parts and p.suffix != '.pyc'
            and 'graph_v3' not in p.name and 'graph_retrieval_v3' not in p.name}


def confine_runtime_writes() -> None:
    """Python audit guard plus local/offline library caches; existing weights read-only.

    This guards Python filesystem APIs, not arbitrary native DLL IO. The task
    invokes no installers, external downloaders, database mutation or subprocess.
    """
    sys.dont_write_bytecode = True
    for name in ('TEMP', 'TMP', 'TMPDIR', 'HF_HOME', 'HF_HUB_CACHE', 'TORCH_HOME', 'XDG_CACHE_HOME',
                 'MPLCONFIGDIR', 'NUMBA_CACHE_DIR', 'TORCHINDUCTOR_CACHE_DIR'):
        destination = artifact_path(TMP / name.lower())
        destination.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(destination)
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false',
                      PGOPTIONS='-c default_transaction_read_only=on', PGCONNECT_TIMEOUT='5')

    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)) and (
                    (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                artifact_path(os.fsdecode(path))
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.utime'}:
            if isinstance(args[0], (str, bytes, os.PathLike)):
                artifact_path(os.fsdecode(args[0]))
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            for path in args[:2]:
                artifact_path(os.fsdecode(path))
    sys.addaudithook(audit)
