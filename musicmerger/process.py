"""Visible subprocess stages with persistent logs and owned-process cancellation."""
import os
import queue
import re
import subprocess
import sys
import threading
import time
from .paths import ROOT


def print_line(value):
    # Model progress bars may contain glyphs unavailable in legacy cmd.exe.
    encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    print(value.encode(encoding, errors='replace').decode(encoding), flush=True)


def run_command(command, log_path, *, watch_log=None, target_seconds=None):
    env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    messages = queue.Queue()
    with log_path.open('x', encoding='utf-8') as log:
        process = subprocess.Popen([str(x) for x in command], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            env=env, creationflags=flags, start_new_session=os.name != 'nt')
        def reader():
            try:
                for line in process.stdout:
                    messages.put(line)
            finally:
                messages.put(None)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        started, last_progress = time.monotonic(), 0
        try:
            while True:
                try:
                    line = messages.get(timeout=2)
                except queue.Empty:
                    elapsed = time.monotonic() - started
                    if elapsed - last_progress < 10:
                        continue
                    last_progress = elapsed
                    detail = f'  Masih berjalan ({elapsed:.0f}s)'
                    if watch_log and watch_log.exists() and target_seconds:
                        with watch_log.open('rb') as source:
                            source.seek(max(0, watch_log.stat().st_size - 8192))
                            tail = source.read().decode('utf-8', errors='replace')
                        times = re.findall(r'time=(\d+):(\d+):([\d.]+)', tail)
                        speeds = re.findall(r'speed=\s*([\d.]+)x', tail)
                        if times:
                            h, m, s = map(float, times[-1])
                            position = h * 3600 + m * 60 + s
                            detail = f'  Encode {min(99.9, position / target_seconds * 100):.1f}%'
                            if speeds and float(speeds[-1]) > 0:
                                detail += f' | sisa kira-kira {max(0, target_seconds-position)/float(speeds[-1]):.0f}s'
                    print_line(detail)
                    continue
                if line is None:
                    break
                log.write(line)
                log.flush()
                print_line(line.rstrip())
            code = process.wait()
        except BaseException:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            raise
        finally:
            thread.join(timeout=2)
            process.stdout.close()
        if code:
            raise RuntimeError(f'Proses gagal (exit {code}). Detail: {log_path}')
