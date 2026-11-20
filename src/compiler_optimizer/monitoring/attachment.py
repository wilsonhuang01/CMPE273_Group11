"""Stream a Docker attach client with bounded memory and an independent lease."""

import selectors
import subprocess
import tempfile
import time

MAX_PROTOCOL_BYTES = 32 * 1024**2


class AttachmentError(RuntimeError):
    pass


def attach(container, payload, watchdog, *, wall_seconds):
    output = bytearray()
    errors = bytearray()
    stopped = None
    deadline = time.monotonic() + wall_seconds + 5
    with tempfile.TemporaryFile() as incoming:
        incoming.write(payload)
        incoming.seek(0)
        process = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", container],
            stdin=incoming,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map() or process.poll() is None:
                    report = watchdog.status()
                    if report["state"] in (
                        "removing",
                        "removed",
                        "cleanup_failed",
                        "ownership_mismatch",
                    ):
                        stopped = report.get("reason", "watchdog_unavailable")
                        break
                    if watchdog.process.poll() is not None:
                        raise AttachmentError("external watchdog exited before cleanup")
                    if time.monotonic() >= deadline:
                        stopped = "external_deadline"
                        break
                    for key, _ in selector.select(timeout=0.05):
                        chunk = key.fileobj.read1(65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            target = output if key.data == "out" else errors
                            if len(output) + len(errors) + len(chunk) > MAX_PROTOCOL_BYTES:
                                stopped = "protocol_output_limit"
                                break
                            target.extend(chunk)
                    if stopped:
                        break
            if stopped:
                watchdog.request_stop(stopped)
                return subprocess.CompletedProcess([], -9, bytes(output), bytes(errors)), stopped
            return subprocess.CompletedProcess(
                [], process.wait(timeout=2), bytes(output), bytes(errors)
            ), None
        finally:
            if process.poll() is None:
                process.kill()  # Only the CLI; the independent watchdog owns container removal.
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()
