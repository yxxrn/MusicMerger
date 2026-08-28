# Batch recovery operator

Run from the original checkout with its Python environment and local models. The
operator calls the unchanged single-folder entrypoint sequentially:
`python -B -m musicmerger <folder> --mode full`. It does not select other folders,
edit lyrics/timings/assets, install dependencies, or schedule an OS task.

```powershell
python -B -m musicmerger.batch run 'D:\asmr video\MusicMerger\inputs' --folders 12,20,35 --job 'D:\asmr video\MusicMerger\batch-recovery-new' --detach
python -B -m musicmerger.batch status 'D:\asmr video\MusicMerger\batch-recovery-new'
python -B -m musicmerger.batch resume 'D:\asmr video\MusicMerger\batch-recovery-new' --detach
```

Replace the example parent with the actual directory containing `12`, `20`, and
`35`. The job path must be **new**, its parent must exist, and it must be outside
every selected input folder. Omit `--detach` to run in the foreground.

Only retry a recorded failure after reviewing its log and addressing the cause:

```powershell
python -B -m musicmerger.batch resume 'D:\asmr video\MusicMerger\batch-recovery-new' --retry-failed --detach
```

## State and ownership

- `job.json`: atomic, flushed/fsynced checkpoints; immutable source paths, sizes
  and SHA-256; folder states `pending`, `running`, `interrupted`, `failed`,
  `verified`; attempt commands/logs, process identity and verification evidence.
- `logs/<folder>-<token>.log`: durable CLI/guardian output for each attempt.
- `logs/<folder>-<token>-process.json`: atomic guardian/CLI PID plus kernel
  creation identity, start/finish and exit status, independent of the owner.
- `logs/<folder>-verification.log`: appended FFmpeg full-decode diagnostics.
- `operator.log` and `launcher.json`: detached operator output and launch receipt.

The launch receipt means a process was submitted; it is **not** completion.
`status` reports the persisted checkpoint, owner/guardian/child identities and
current liveness. `updated` is a checkpoint timestamp, not a death detector.

Kernel byte locks on `owner.lock` and `child.lock` exclude duplicate work in one
job. The guardian additionally owns `<folder>/MusicMerger-output/.batch.lock`,
excluding another batch guardian in that folder. Locks are released by the OS
after process death; lock files intentionally remain and must not be deleted.
The existing single-folder CLI has its own publication lock, but does not join
the batch render lock: do not run it manually against an active batch folder.

On Windows, creation identity comes from `GetProcessTimes`, so PID reuse is not
mistaken for the old process. Inspection failure is an error, never evidence of
death. A live child blocks resume even if its owner has died; no heartbeat or
timeout kills it. A persisted attempt token and child lease reject a delayed
guardian after recovery has invalidated its launch intent.

The guardian joins its own Windows JobObject before launching the CLI. Its
descendants belong to that job, with kill-on-close enabled. The guardian is
independent of the owner: closing/killing the owner does not intentionally kill
an active render. Foreground Ctrl+C explicitly terminates the owned guardian,
which terminates its CLI/subprocess tree, then records `interrupted`. Do not use
process-name-wide kill commands. Hidden detach uses `DETACHED_PROCESS`, a new
process group, redirected durable output and hidden startup information.

After machine restart, run `resume` explicitly. There is no boot-time scheduler,
automatic restart, or guarantee of continued execution during shutdown. Windows
JobObject ownership is integration-tested; the POSIX fallback uses process
groups for explicit interruption and is not a Windows-equivalent descendant
crash-containment guarantee.

## Resume and verification gates

Every selected source is checked before any child starts. The source set is
exactly one MP4, MP3 and Markdown plus reviewed `youtube-metadata.json` whose MD
name/hash matches. Missing, changed or linked/junction inputs are rejected.
Hugging Face/Transformers offline environment flags are forced for all children;
other environment settings are inherited. Required local models must already
exist. The batch operator never downloads models or changes the model defaults.

Resume reconciles a dead owner using the independent child receipt. A completed
nonzero exit remains `failed`; it is not implicitly retried. An unfinished dead
attempt becomes `interrupted`. A zero exit without a valid package is also a
failure, not `verified`. A failed or interrupted attempt that already published
a valid managed package can be adopted without rerendering.

Before adopting or skipping an output, the operator checks:

1. Both publication manifests, exact HASIL filenames and all output hashes.
2. Full-run source audio/MD hashes, thumbnail source report for MP4/MP3/MD/JSON,
   report JPEG hash, and exact TXT title/tags from approved source metadata.
3. JPEG decode, 1280x720 dimensions and size under 2 MiB.
4. FFprobe format and stream durations within 0.25 seconds of the source MP3;
   exactly one H.264 video and AAC audio; positive dimensions/frame rate;
   decoded frame count consistent with duration (three-frame tolerance).
5. Full audio/video decode with `ffmpeg -xerror`, then source and output hashes
   again before saving `verified`.

An existing final is accepted even if its single-folder status still says
`running`, because publication may have succeeded immediately before a crash.
The manifests and source provenance must still pass. A previously verified run,
thumbnail run, output hash or manifest hash may not change on resume.

Unknown/partial outputs, stale publication locks and changed sources are
preserved for review; the operator does not delete them or guess repairs. A
verification error can stop reconciliation before later folders run. Explicit
`--retry-failed` does not authorize replacing unknown or changed outputs.

One pre-render layout is allowed: with **neither** publication manifest present,
`HASIL` may contain exactly `youtube-upload.txt` whose UTF-8 text matches the
approved source metadata. It is treated as fresh input, not a published package.
The operator only reads that TXT and does not delete or rewrite it. Any additional
file, mismatched TXT or either manifest restores the normal verification gate.

Exit codes: `0` for all verified (or a submitted detach/status request), `1` for
completed foreground work with failed folders, `2` for validation/ownership
errors, `130` for an explicit foreground interrupt.

## Verification performed

```powershell
python -B -m unittest discover -s tests -p test_batch.py -v
```

The focused suite uses temporary directories, tiny synthetic FFmpeg media and
harmless Python children. It covers atomic-write failure, source changes, live
locks/PID reuse, stale launch tokens, dead-owner recovery and retry policy,
owned-tree termination, public hidden detach after launcher exit, provenance,
real full-decode/JPEG verification, and unchanged completed resume. It does not
render or mutate any production folder.
