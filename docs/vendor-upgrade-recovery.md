# UU host upgrade recovery

UUWay 1.5.3 adds recovery for the vendor UU host's own upgrade flow. UUWay
package updates continue to use their existing package backup, health checks
and rollback. Neither mechanism guarantees compatibility with unreleased UU.

## Recovery point

The native worker creates a private, durable ZIP snapshot of the current
program before starting Wine. It includes `bin` and the root executables/DLLs,
with only the version fields needed from Wine's registry. Accounts, settings,
downloads and logs outside the program payload are not copied or restored.
UUWay loader links and terminal credentials are excluded. The original vendor
ConPTY DLL is saved so rollback can attach the current terminal adapter.

Snapshots have a SHA-256 checksum, bounded sizes and validated relative paths.
Snapshot creation checks that the source payload and version fields did not
change during the copy. A damaged or untrusted snapshot is rejected before
active program files are changed. The latest two snapshots are retained.

## Upgrade and health checks

The worker records a pending upgrade before waiting for the installer. A
directory rename is recognized by the original loader-link identities, even
when its new name is not a version number. Unknown component replacements
still stop the worker, with a pending vendor update recovered from its snapshot.

The installer gets up to 180 seconds to finish. Startup then requires successful
GUI, service and input adaptation. A process starting successfully is not enough:
a rejected native hook immediately fails validation. The candidate has up to
180 seconds to produce capture-frame evidence belonging to its live UU server
and the pinned capture adapter. It must also remain ready for 30 seconds.
Only then can it replace the previous recovery point.

A frame acknowledgement proves local capture allocation acceptance, not
remote decoding or presentation. A client must connect during the validation
window; if no frame can be verified, UUWay returns to the previous program.
This deliberately favors a recoverable host over an unverified new version.

If a new snapshot cannot be saved after successful frame validation, the old
recovery point is retained and the working candidate stays running while
snapshot promotion is retried. Housekeeping failures do not stop the worker.

## Failure and interruption

After a failed install or candidate startup, the worker stops only its owned
Wine prefix, verifies its snapshot, stages the restored code and moves the failed
payload aside. It restores the old version fields while preserving other registry
values. The supervisor restores the terminal entrypoint and restarts the host.
Only the latest two recorded failed payloads are retained.

Pending installation, startup and restoration phases are persisted. On the next
start after a process failure or power interruption, recovery runs before the
usual missing-component checks. A second interruption during restoration is
handled through the same restore path. A live Wine prefix is never overwritten.

Run `uuway doctor` for recovery-point status, pending validation or a previous
rollback. Events are in:

```bash
journalctl --user -u uu-native-bridge
```

Recovery needs a readable snapshot and enough working disk space. It cannot
repair arbitrary disk damage or promise compatibility with future changes to
UU's network, account or capture interfaces. User data remains untouched by the
program rollback.

## Validation

The regression suite covers non-version directory names, incomplete payloads,
process-scoped installer detection, installer and capture timeouts, rejected GUI
adaptation, stable readiness, current-frame gating, corrupt snapshots, unsafe
archive paths, interrupted rollback, preservation of user data, failed checkpoint
writes and bounded retention.

Live fault drills on the maintained installation removed `bin` during a recorded
upgrade and substituted a valid Windows program whose GUI adapter rejected it.
Both cases restored the verified UU program and remote-terminal owner without
requiring a reinstall or a systemd restart loop.
