"""Durable sequential batch operator: run, status, resume (optional hidden detach)."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .batch_process import (BusyError, Lease, hidden_options, own_windows_tree,
                            process_alive, process_identity, terminate_guardian)
from .batch_verify import (check_sources, digest, has_package, read_json, safe_path,
                           source_snapshot, verify_package)
from .paths import ROOT


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, payload):
    path = safe_path(path)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_state(job):
    state = read_json(safe_path(Path(job) / 'job.json'))
    if (not isinstance(state, dict) or state.get('schema') != 1 or
            not isinstance(state.get('folders'), list) or not state['folders']):
        raise ValueError('Invalid batch state; preserve job.json for review')
    if Path(state['job']).resolve() != Path(job).resolve():
        raise ValueError('Job directory moved; stored source/attempt paths require operator review')
    return state


def create_job(parent, folders, job):
    parent, job = safe_path(parent), safe_path(job)
    if not parent.is_dir():
        raise ValueError(f'Parent directory missing: {parent}')
    if not folders or len(set(folders)) != len(folders):
        raise ValueError('Select a nonempty list of unique folder names')
    if job.exists():
        raise FileExistsError(f'Job directory already exists: {job}; use resume')
    rows = []
    for name in folders:
        if not name or name in ('.', '..') or any(c in name for c in '/\\:'):
            raise ValueError(f'Invalid folder name: {name}')
        folder = safe_path(parent / name)
        if folder == job or folder in job.parents or job in folder.parents:
            raise ValueError('Job must be outside each input folder')
        rows.append(dict(name=name, path=str(folder), sources=source_snapshot(folder),
                         status='pending', attempts=[], token=None))
    state = dict(schema=1, job=str(job), parent=str(parent), created=now(), updated=now(),
                 owner=None, folders=rows)
    job.mkdir()  # Exclusive reservation; never reuse or remove operator data.
    (job / 'logs').mkdir()
    atomic_json(job / 'job.json', state)
    return state


def _save(job, state):
    state['updated'] = now()
    atomic_json(job / 'job.json', state)


def _offline_environment():
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
               PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def _assert_no_recorded_child(job, state):
    for row in state['folders']:
        for attempt in row['attempts']:
            record_path = Path(attempt.get('process_record', job / 'missing-process-record'))
            if record_path.exists():
                record = read_json(safe_path(record_path))
                for key in ('guardian', 'child'):
                    if process_alive(record.get(key)):
                        raise BusyError(f'Live {key} remains for folder {row["name"]}: {record[key]}')


def launch_guardian(job, token, log):
    return subprocess.Popen([sys.executable, '-B', '-m', 'musicmerger.batch', '_child', str(job), token],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            cwd=ROOT, env=_offline_environment(), close_fds=True, **hidden_options())


def guardian(job, token):
    """Own the child lease/tree independently of the launcher; never write job.json."""
    job = safe_path(job)
    with Lease(job / 'child.lock'):
        state = read_state(job)
        matches = [row for row in state['folders'] if row.get('token') == token and row['status'] == 'running']
        if len(matches) != 1:
            raise ValueError('Attempt cancelled before child start; refusing stale launch')
        row = matches[0]
        attempt = row['attempts'][-1]
        if attempt['token'] != token:
            raise ValueError('Attempt token mismatch')
        record_path = safe_path(attempt['process_record'])
        record = dict(token=token, guardian=process_identity(os.getpid()), started=now())
        atomic_json(record_path, record)
        try:
            # Assign guardian before the CLI exists; parent death does not affect this job.
            tree_handle = own_windows_tree()
            source_output = safe_path(Path(row['path']) / 'MusicMerger-output')
            source_output.mkdir(exist_ok=True)
            with Lease(source_output / '.batch.lock'):
                check_sources(row)
                if has_package(row):
                    # Another reviewed job may have published since the owner
                    # checked. Let the owner verify/adopt; never rerender over it.
                    code = 0
                    record['publication_appeared'] = True
                else:
                    with safe_path(attempt['log']).open('ab', buffering=0) as log:
                        child = subprocess.Popen(attempt['command'], stdin=subprocess.DEVNULL,
                                                 stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                                                 env=_offline_environment(), close_fds=True)
                        record['child'] = process_identity(child.pid)
                        atomic_json(record_path, record)
                        code = child.wait()
            record.update(returncode=code, finished=now())
            atomic_json(record_path, record)
            return code
        except BaseException as exc:
            record.update(error=f'{type(exc).__name__}: {exc}', finished=now(), returncode=1)
            atomic_json(record_path, record)
            raise


def execute(job, *, retry_failed=False, command_factory=None):
    """Run or reconcile a job. command_factory is an internal harmless-test seam."""
    job = safe_path(job)
    command_factory = command_factory or (lambda folder: [sys.executable, '-B', '-m', 'musicmerger',
                                                          str(folder), '--mode', 'full'])
    with Lease(job / 'owner.lock'):
        state = read_state(job)
        with Lease(job / 'child.lock'):
            _assert_no_recorded_child(job, state)
            if process_alive(state.get('owner')):
                raise BusyError(f'Owner is still live: {state["owner"]}')
            # Invalidate old spawn tokens under the same lease acquired by a guardian.
            for row in state['folders']:
                if row['status'] == 'running':
                    row.update(status='interrupted', token=None)
                    if row['attempts']:
                        attempt = row['attempts'][-1]
                        record_path = Path(attempt.get('process_record', job / 'missing-process-record'))
                        record = read_json(record_path) if record_path.exists() else {}
                        if record.get('finished'):
                            attempt.update(returncode=record.get('returncode'), finished=record['finished'])
                            # Even exit zero is not a result. The package adoption
                            # below must validate it; absent output needs review.
                            row.update(status='failed', error=record.get('error') or
                                       f'Unverified completed child exited {record.get("returncode")}; see {attempt.get("log")}')
                        else:
                            attempt.setdefault('interrupted', now())
            state['owner'] = process_identity(os.getpid())
            state.pop('error', None)
            _save(job, state)
        current_process = current_identity = None
        current_row = None
        try:
            # Check every source before launching any folder in the job.
            for row in state['folders']:
                check_sources(row)
            for row in state['folders']:
                current_row = row
                verification_log = job / 'logs' / f'{row["name"]}-verification.log'
                if has_package(row):
                    verified = verify_package(row, verification_log)
                    previous = row.get('verification')
                    if previous and any(previous.get(key) != verified.get(key) for key in
                                        ('hashes', 'run', 'thumbnail_run', 'manifests')):
                        raise ValueError(f'Verified output changed for folder {row["name"]}; review required')
                    row.update(status='verified', verification=verified, verified_at=now(), token=None)
                    row.pop('error', None)
                    _save(job, state)
                    continue
                if row['status'] == 'verified':
                    raise ValueError(f'Verified package missing for folder {row["name"]}; refusing automatic rerender')
                if row['status'] == 'failed' and not retry_failed:
                    continue
                token = uuid.uuid4().hex
                attempt = dict(token=token, started=now(), command=command_factory(Path(row['path'])),
                               log=str(job / 'logs' / f'{row["name"]}-{token}.log'),
                               process_record=str(job / 'logs' / f'{row["name"]}-{token}-process.json'))
                row['attempts'].append(attempt)
                row.update(status='running', token=token)
                row.pop('error', None)
                _save(job, state)  # Commit intent before spawning. Stale guardians check this token.
                with Path(attempt['log']).open('ab', buffering=0) as log:
                    current_process = launch_guardian(job, token, log)
                    current_identity = process_identity(current_process.pid)
                    attempt['guardian'] = current_identity
                    _save(job, state)
                    returncode = current_process.wait()
                current_process = current_identity = None
                record_path = Path(attempt['process_record'])
                record = read_json(record_path) if record_path.exists() else {}
                attempt.update(returncode=record.get('returncode', returncode), finished=now())
                row['token'] = None
                if returncode:
                    row.update(status='failed', error=record.get('error') or
                               f'Child exited {attempt["returncode"]}; see {attempt["log"]}')
                else:
                    try:
                        verified = verify_package(row, verification_log)
                        row.update(status='verified', verification=verified, verified_at=now())
                    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                        row.update(status='failed', error=f'Output verification failed: {exc}')
                _save(job, state)
            return state
        except KeyboardInterrupt:
            if current_process is not None:
                terminate_guardian(current_process, current_identity)
            if current_row is not None and current_row['status'] == 'running':
                current_row.update(status='interrupted', token=None, error='Operator interrupted owned process tree')
            state['error'] = 'Interrupted; explicit resume is required'
            raise
        except BaseException as exc:
            # Do not kill a live child merely because saving a checkpoint failed.
            state['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            state['owner'] = None
            _save(job, state)


def detach(job, *, retry_failed=False):
    job = safe_path(job)
    # Fast fail for live work. The detached worker also acquires these leases.
    with Lease(job / 'owner.lock'), Lease(job / 'child.lock'):
        _assert_no_recorded_child(job, read_state(job))
    command = [sys.executable, '-B', '-m', 'musicmerger.batch', 'resume', str(job)]
    if retry_failed:
        command.append('--retry-failed')
    with (job / 'operator.log').open('ab', buffering=0) as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   cwd=ROOT, env=_offline_environment(), close_fds=True, **hidden_options())
    identity = process_identity(process.pid)
    atomic_json(job / 'launcher.json', dict(worker=identity, launched=now(), command=command))
    return dict(job=str(job), state=str(job / 'job.json'), operator_log=str(job / 'operator.log'),
                worker=identity, message='Detached launch submitted; status/resume verifies actual outcome')


def status(job):
    state = read_state(job)
    rows = []
    for row in state['folders']:
        attempt = row['attempts'][-1] if row['attempts'] else {}
        record_path = Path(attempt.get('process_record', Path(job) / 'missing-process-record'))
        record = read_json(safe_path(record_path)) if record_path.exists() else {}
        rows.append(dict(name=row['name'], status=row['status'], attempts=len(row['attempts']),
                         error=row.get('error'), output=row.get('verification', {}).get('video'),
                         log=attempt.get('log'), guardian=record.get('guardian'), child=record.get('child'),
                         guardian_alive=process_alive(record.get('guardian')),
                         child_alive=process_alive(record.get('child'))))
    return dict(job=state['job'], updated=state['updated'], owner=state.get('owner'),
                owner_alive=process_alive(state.get('owner')), error=state.get('error'),
                folders=rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    run = sub.add_parser('run')
    run.add_argument('parent', type=Path)
    run.add_argument('--folders', required=True, help='Exact folder names separated by commas')
    run.add_argument('--job', required=True, type=Path, help='New job directory outside input folders')
    run.add_argument('--detach', action='store_true')
    resume = sub.add_parser('resume')
    resume.add_argument('job', type=Path)
    resume.add_argument('--retry-failed', action='store_true')
    resume.add_argument('--detach', action='store_true')
    show = sub.add_parser('status')
    show.add_argument('job', type=Path)
    child = sub.add_parser('_child', help='internal guardian; not for manual use')
    child.add_argument('job', type=Path)
    child.add_argument('token')
    args = parser.parse_args(argv)
    try:
        if args.action == '_child':
            return guardian(args.job, args.token)
        if args.action == 'status':
            print(json.dumps(status(args.job), ensure_ascii=False, indent=2), flush=True)
            return 0
        if args.action == 'run':
            create_job(args.parent, [s.strip() for s in args.folders.split(',')], args.job)
        if args.detach:
            result = detach(args.job, retry_failed=getattr(args, 'retry_failed', False))
            code = 0
        else:
            state = execute(args.job, retry_failed=getattr(args, 'retry_failed', False))
            result = status(args.job)
            code = 0 if all(row['status'] == 'verified' for row in state['folders']) else 1
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return code
    except KeyboardInterrupt:
        print('Interrupted. Owned child stopped; use resume explicitly.', file=sys.stderr, flush=True)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
