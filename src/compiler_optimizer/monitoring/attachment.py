"""Stream a Docker attach client with bounded memory and an independent lease."""

import os
import selectors
import subprocess
import time

STARTUP_READY = b"compiler-optimizer-ready-v1\n"

MAX_PROTOCOL_BYTES = 32 * 1024**2


class AttachmentError(RuntimeError):
    pass


def attach(container, payload, watchdog, *, wall_seconds, on_ready=None):
    output = bytearray()
    errors = bytearray()
    stopped = None
    deadline = time.monotonic() + wall_seconds + 5
    pending = memoryview(payload)
    ready = on_ready is None
    handshake = bytearray()
    process = subprocess.Popen(
        ["docker", "start", "--attach", "--interactive", container],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "out"), (process.stderr, "err")):
                selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(process.stdin.fileno(), False)
            if ready:
                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
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
                    if key.data == "in":
                        if pending:
                            try:
                                count = os.write(process.stdin.fileno(), pending[:65536])
                                pending = pending[count:]
                            except BrokenPipeError:
                                pending = memoryview(b"")
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        if key.data == "out" and not ready:
                            handshake.extend(chunk)
                            if not STARTUP_READY.startswith(handshake):
                                raise AttachmentError("invalid supervisor startup handshake")
                            if handshake == STARTUP_READY:
                                on_ready()
                                ready = True
                                selector.register(process.stdin, selectors.EVENT_WRITE, "in")
                            continue
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
        if not ready:
            raise AttachmentError("supervisor exited before startup handshake")
        return subprocess.CompletedProcess(
            [], process.wait(timeout=2), bytes(output), bytes(errors)
        ), None
    finally:
        if process.poll() is None:
            process.kill()  # Only the CLI; the independent watchdog owns container removal.
        process.wait(timeout=3)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
