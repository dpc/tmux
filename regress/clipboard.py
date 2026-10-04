#!/usr/bin/env python3
"""OSC5522 integration tests; only private temporary tmux servers and PTYs."""

import base64
import hashlib
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty


def helper(path):
    tty.setraw(0)
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(path)
    sock.sendall(b"READY")
    sock.setblocking(False)
    pending = b""
    while True:
        ready, writable, _ = select.select(
            [sock] + ([0] if len(pending) < 65536 else []),
            [sock] if pending else [], [])
        if writable:
            pending = pending[sock.send(pending):]
        for source in ready:
            data = os.read(0, 65536) if source == 0 else sock.recv(65536)
            if not data:
                return
            if source == 0:
                pending += data
            else:
                while data:
                    data = data[os.write(1, data):]


class Stream:
    def __init__(self, fd):
        self.fd = fd
        self.buffer = b""

    def read(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError()
            self.buffer += chunk

    def until(self, needle, timeout=5):
        end = time.monotonic() + timeout
        while needle not in self.buffer:
            if time.monotonic() >= end:
                raise AssertionError("missing %r in %r" % (needle, self.buffer[-500:]))
            self.read(0.05)
        stop = self.buffer.index(needle) + len(needle)
        result, self.buffer = self.buffer[:stop], self.buffer[stop:]
        return result

    def frame(self, timeout=5):
        self.until(b"\x1b]5522;", timeout)
        body = self.until(b"\x1b\\", timeout)[:-2]
        meta, sep, payload = body.partition(b";")
        fields = dict(field.split(b"=", 1) for field in meta.split(b":"))
        return fields, payload if sep else None

    def quiet(self, seconds=0.2):
        self.read(seconds)
        result, self.buffer = self.buffer, b""
        return result

    def drain(self):
        result = b""
        while True:
            chunk = self.quiet(0.05)
            if not chunk:
                return result
            result += chunk


def osc(meta, data=None, bel=False):
    return b"\x1b]5522;" + meta + (b";" + data if data is not None else b"") + (
        b"\x07" if bel else b"\x1b\\")


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def response(ident, status, data=None, mime=None, kind=b"read"):
    meta = b"type=" + kind + b":id=" + ident + b":status=" + status
    if mime is not None:
        meta += b":mime=" + base64.b64encode(mime)
    return osc(meta, data)


class Server:
    def __init__(self, binary, root, name):
        self.binary = binary
        self.root = root
        self.args = [binary, "-S", os.path.join(root, name), "-f", "/dev/null"]
        self.children = []
        self.sockets = []
        self.count = 0

    def run(self, *args, check=True):
        return subprocess.run(self.args + list(args), check=check, cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def command(self, *args):
        return subprocess.Popen(self.args + list(args), cwd=self.root,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def pane(self, first=False):
        self.count += 1
        path = os.path.join(self.root, "app-%s-%d" % (os.path.basename(self.args[2]), self.count))
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen()
        listener.settimeout(5)
        command = shlex.join([sys.executable, os.path.abspath(__file__), "--helper", path])
        if first:
            pane = self.run("-vv", "new-session", "-d", "-s", "clip", "-P",
                            "-F", "#{pane_id}", command).strip().decode()
        else:
            pane = self.run("split-window", "-d", "-P", "-F", "#{pane_id}",
                            command).strip().decode()
        sock, _ = listener.accept()
        listener.close()
        self.sockets.append(sock)
        stream = Stream(sock.fileno())
        stream.until(b"READY")
        return pane, sock, stream

    def attach(self, support=True, verified=True):
        pid, fd = os.forkpty()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("TMUX", None)
            os.chdir(self.root)
            args = self.args + ["attach-session", "-t", "clip"]
            if verified:
                args += ["-f", "clipboard-fence"]
            os.execv(self.binary, args)
        self.children.append((pid, fd))
        stream = Stream(fd)
        stream.until(b"\x1b[?5522$p")
        if support is not None:
            os.write(fd, b"\x1b[?5522;" + (b"2" if support else b"0") + b"$y")
        return fd, stream

    def close(self):
        self.run("kill-server", check=False)
        for sock in self.sockets:
            sock.close()
        for pid, fd in self.children:
            os.close(fd)
            os.waitpid(pid, 0)


def fence(fd, stream):
    fields, payload = stream.frame()
    assert fields[b"type"] == b"read" and payload == b"Lg=="
    assert b"pw" not in fields and b"name" not in fields
    ident = fields[b"id"]
    os.write(fd, response(ident, b"OK") +
             response(ident, b"DATA", b"dGV4dC9wbGFpbg==", b".") +
             response(ident, b"DONE"))


def command_ok(command):
    stdout, stderr = command.communicate(timeout=5)
    assert command.returncode == 0, (stdout, stderr)


def main(binary):
    binary = os.path.abspath(binary)
    with tempfile.TemporaryDirectory(prefix="tmux-clipboard-") as root:
        server = Server(binary, root, "outer")
        inner = Server(binary, root, "inner")
        try:
            pane, app, incoming = server.pane(first=True)
            fd, terminal = server.attach()
            app.sendall(b"\x1b[?5522$p")
            incoming.until(b"\x1b[?5522;2$y")
            app.sendall(b"\x1b[?2004h\x1b[?5522h")
            terminal.until(b"\x1b[?5522h")

            # Current dot-MIME offer, primary, unchanged token/name/location.
            token = base64.b64encode(b"private-test-grant-never-log")
            offer = (osc(b"type=read:status=OK:pw=" + token + b":loc=primary", bel=True) +
                     osc(b"type=read:status=DATA:mime=Lg==",
                         base64.b64encode(b"image/png text/plain application/x-test")) +
                     osc(b"type=read:status=DONE"))
            for byte in offer:
                os.write(fd, bytes([byte]))
            incoming.until(b"status=DONE\x1b\\")
            request = (b"type=read:id=original:loc=primary:pw=" + token +
                       b":name=UGFzdGUgZXZlbnQ=")
            app.sendall(osc(request, base64.b64encode(b"image/png text/plain")))
            fields, payload = terminal.frame()
            ident = fields[b"id"]
            assert ident != b"original" and fields[b"pw"] == token
            assert fields[b"loc"] == b"primary" and fields[b"name"] == b"UGFzdGUgZXZlbnQ="
            assert payload == base64.b64encode(b"image/png text/plain")
            os.write(fd, response(ident, b"OK") +
                     response(ident, b"DATA", b"YQ==", b"image/png") +
                     response(ident, b"DATA", b"YmM=", b"image/png") +
                     response(ident, b"DATA", b"eAo=", b"text/plain") +
                     response(ident, b"DONE"))
            chunks = []
            for status in (b"OK", b"DATA", b"DATA", b"DATA", b"DONE"):
                fields, payload = incoming.frame()
                assert fields[b"id"] == b"original" and fields[b"status"] == status
                if status == b"DATA":
                    chunks.append(base64.b64decode(payload))
            assert chunks == [b"a", b"bc", b"x\n"]
            assert b"\x1b[200~" not in incoming.quiet()

            # Ordinary ungranted reads use terminal permission, not a mux policy.
            app.sendall(osc(b"type=read:id=ambient", b"Lg=="))
            fields, _ = terminal.frame()
            ident = fields[b"id"]
            os.write(fd, response(ident, b"EPERM"))
            assert incoming.frame()[0][b"status"] == b"EPERM"

            # Unknown/stale replies and malformed/oversized frames cannot be keys.
            os.write(fd, response(ident, b"DATA", b"c2VjcmV0", b"text/plain"))
            assert incoming.quiet() == b""
            os.write(fd, osc(b"nonsense"))
            assert incoming.quiet() == b""
            huge = b"\x1b]5522;" + b"x" * (1024 * 1024 + 100) + b"\x1b\\"
            sender = threading.Thread(target=lambda: write_all(fd, huge))
            sender.start()
            sender.join(5)
            assert not sender.is_alive()
            assert incoming.quiet() == b""
            os.write(fd, b"z")
            incoming.until(b"z")
            server.run("set-option", "-s", "escape-time", "50")
            os.write(fd, b"\x1b]")
            incoming.until(b"\x1b]", timeout=1)
            os.write(fd, b"\x1b[?")
            incoming.until(b"\x1b[?", timeout=1)
            os.write(fd, b"\x1b[?5522;" + b"0" * 100 + b"$y")
            assert incoming.quiet() == b""

            # Dropping a malformed chunk must also drop the eventual DONE;
            # otherwise the consumer could publish a truncated valid prefix.
            for bad_kind in ("nul", "esc-bel", "missing-status", "wrong-type",
                             "duplicate-ok", "oversized"):
                app.sendall(osc(b"type=read:id=corrupt-read", b"dGV4dC9wbGFpbg=="))
                ident = terminal.frame()[0][b"id"]
                os.write(fd, response(ident, b"OK") +
                         response(ident, b"DATA", b"cHJlZml4", b"text/plain"))
                assert incoming.frame()[0][b"status"] == b"OK"
                assert incoming.frame()[0][b"status"] == b"DATA"
                if bad_kind == "nul":
                    bad = response(ident, b"DATA", b"Y\x00Q==", b"text/plain")
                elif bad_kind == "esc-bel":
                    bad = response(ident, b"DATA", b"YQ==", b"text/plain")[:-2] + b"\x1b\x07"
                elif bad_kind == "missing-status":
                    bad = osc(b"type=read:id=" + ident +
                              b":mime=dGV4dC9wbGFpbg==", b"YQ==")
                elif bad_kind == "wrong-type":
                    bad = response(ident, b"DATA", b"YQ==", b"text/plain", b"write")
                elif bad_kind == "duplicate-ok":
                    bad = response(ident, b"OK")
                else:
                    bad = response(ident, b"DATA", b"A" * (1024 * 1024 + 1),
                                   b"text/plain")
                sender = threading.Thread(target=lambda: write_all(
                    fd, bad + response(ident, b"DONE")))
                sender.start()
                sender.join(5)
                assert not sender.is_alive()
                assert incoming.drain() == b"", bad_kind

            # Arbitrary-MIME write, split base64 across packets, aliases, IDs.
            app.sendall(osc(b"type=write:id=write-original"))
            fields, _ = terminal.frame()
            write_id = fields[b"id"]
            packets = [
                (b"type=wdata:mime=YXBwbGljYXRpb24veC10ZXN0", b"Y"),
                (b"type=wdata:mime=YXBwbGljYXRpb24veC10ZXN0", b"WJj"),
                (b"type=walias:mime=YXBwbGljYXRpb24veC10ZXN0",
                 base64.b64encode(b"application/x-alias text/plain")),
                (b"type=wdata", None),
            ]
            for meta, data in packets:
                app.sendall(osc(meta, data))
                fields, got = terminal.frame()
                assert fields[b"id"] == write_id and got == data
            os.write(fd, response(write_id, b"DONE", kind=b"write"))
            fields, _ = incoming.frame()
            assert fields[b"id"] == b"write-original" and fields[b"status"] == b"DONE"

            # Error abort: continuation drain until next opening write.
            app.sendall(osc(b"type=write:id=denied"))
            fields, _ = terminal.frame()
            os.write(fd, response(fields[b"id"], b"EPERM", kind=b"write"))
            assert incoming.frame()[0][b"status"] == b"EPERM"
            app.sendall(osc(b"type=wdata:mime=dGV4dC9wbGFpbg==", b"c2VjcmV0"))
            assert b"\x1b]5522;" not in terminal.quiet()
            app.sendall(osc(b"type=write:id=malformed"))
            ident = terminal.frame()[0][b"id"]
            app.sendall(osc(b"type=wdata:mime=YQ==:mime=Yg==", b"YQ=="))
            fields, payload = terminal.frame()
            assert fields[b"id"] == ident and payload == b"!"
            assert incoming.frame()[0][b"status"] == b"EINVAL"
            app.sendall(osc(b"type=wdata"))
            assert b"\x1b]5522;" not in terminal.quiet()
            os.write(fd, response(ident, b"EINVAL", kind=b"write"))
            assert incoming.quiet() == b""

            # Background panes may request reads, but cannot steal paste grants.
            other, app2, incoming2 = server.pane()
            os.write(fd, offer)
            incoming.until(b"status=DONE\x1b\\")
            app2.sendall(osc(request, b"Lg=="))
            assert incoming2.frame()[0][b"status"] == b"EPERM"
            app2.sendall(osc(b"type=read:id=background", b"Lg=="))
            fields, _ = terminal.frame()
            ident = fields[b"id"]
            app.sendall(osc(b"type=write:id=busy"))
            fields, _ = incoming.frame()
            assert fields[b"id"] == b"busy" and fields[b"status"] == b"EBUSY"
            os.write(fd, response(ident, b"EBUSY"))
            assert incoming2.frame()[0][b"id"] == b"background"

            # Same-mode focus switch revokes grants, cancels old replies.
            app2.sendall(b"\x1b[?5522h")
            app.sendall(osc(b"type=read:id=old-pane", b"Lg=="))
            ident = terminal.frame()[0][b"id"]
            server.run("select-pane", "-t", other)
            terminal.until(b"\x1b[?5522l")
            terminal.until(b"\x1b[?5522h")
            os.write(fd, response(ident, b"OK") + response(ident, b"DONE"))
            assert incoming.quiet() == b"" and incoming2.quiet() == b""
            app2.sendall(osc(request, b"Lg=="))
            assert incoming2.frame()[0][b"status"] == b"EPERM"
            server.run("select-pane", "-t", pane)
            terminal.until(b"\x1b[?5522h")

            # Unsolicited packet begun in the old pane must not finish in the new.
            os.write(fd, b"\x1b]5522;type=read:status=OK:pw=" + token)
            time.sleep(0.1)
            server.run("select-pane", "-t", other)
            terminal.until(b"\x1b[?5522h")
            os.write(fd, b"\x1b\\" + osc(b"type=read:status=DONE"))
            assert incoming.quiet() == b"" and incoming2.quiet() == b""
            server.run("select-pane", "-t", pane)
            terminal.until(b"\x1b[?5522h")

            # Observable focus-out/in invalidates grants even in the same pane.
            os.write(fd, offer)
            incoming.until(b"status=DONE\x1b\\")
            os.write(fd, b"\x1b[O")
            terminal.until(b"\x1b[?5522l")
            os.write(fd, b"\x1b[I")
            terminal.until(b"\x1b[?5522h")
            app.sendall(osc(request, b"Lg=="))
            assert incoming.frame()[0][b"status"] == b"EPERM"

            # Multi-client attachment: token pins the originating terminal.
            fd2, terminal2 = server.attach()
            terminal2.until(b"\x1b[?5522h")
            fresh = base64.b64encode(b"second-attachment-unique-grant")
            offer = offer.replace(token, fresh)
            request = request.replace(token, fresh)
            os.write(fd, offer)
            incoming.until(b"status=DONE\x1b\\")
            app.sendall(osc(request, b"Lg=="))
            ident = terminal.frame()[0][b"id"]
            assert b"\x1b]5522;" not in terminal2.quiet()
            os.write(fd, response(ident, b"EPERM"))
            incoming.frame()
            os.write(fd2, b"\x02d")  # Detach only the private second client.
            fence(fd2, terminal2)
            time.sleep(0.1)

            # Unknown tokens are unchanged terminal policy inputs; same-owner
            # missing-name/listing reads must not spend the unused paste grant.
            unknown = base64.b64encode(b"unknown-token-for-ambient-policy")
            app.sendall(osc(b"type=read:id=unknown:pw=" + unknown, b"Lg=="))
            fields, _ = terminal.frame()
            assert fields[b"pw"] == unknown
            os.write(fd, response(fields[b"id"], b"OK") +
                     response(fields[b"id"], b"DONE"))
            incoming.frame()
            incoming.frame()
            app.sendall(osc(b"type=read:id=missing-name:loc=primary:pw=" + fresh, b"Lg=="))
            fields, _ = terminal.frame()
            assert fields[b"pw"] == fresh and b"name" not in fields
            os.write(fd, response(fields[b"id"], b"OK") +
                     response(fields[b"id"], b"DONE"))
            incoming.frame()
            incoming.frame()
            app.sendall(osc(request, b"Lg=="))
            fields, _ = terminal.frame()
            os.write(fd, response(fields[b"id"], b"EPERM"))
            incoming.frame()

            # Inbound backpressure: stop reading the app until both socket and
            # pane queues fill, then verify every independently padded chunk.
            app.sendall(osc(b"type=read:id=large-in"))
            ident = terminal.frame()[0][b"id"]
            block = base64.b64encode(b"r" * 4096)
            packet_in = response(ident, b"DATA", block, b"application/x-test")
            def produce_in():
                packets = [response(ident, b"OK")] + [packet_in] * 1024 + [
                    response(ident, b"DONE")]
                for packet in packets:
                    while packet:
                        packet = packet[os.write(fd, packet):]
            sender = threading.Thread(target=produce_in)
            sender.start()
            time.sleep(0.3)
            assert sender.is_alive(), "inbound producer did not encounter backpressure"
            assert incoming.frame()[0][b"status"] == b"OK"
            for _ in range(1024):
                fields, got = incoming.frame()
                assert fields[b"id"] == b"large-in" and got == block
            assert incoming.frame()[0][b"status"] == b"DONE"
            sender.join(5)
            assert not sender.is_alive()
            os.write(fd, b"k")
            incoming.until(b"k")

            # Cancellation must release paused tty input, not strand keyboard.
            app.sendall(osc(b"type=read:id=cancel-pressure"))
            ident = terminal.frame()[0][b"id"]
            packet_in = response(ident, b"DATA", block, b"application/x-test")
            sender = threading.Thread(target=produce_in)
            sender.start()
            time.sleep(0.3)
            assert sender.is_alive()
            server.run("select-pane", "-t", other)
            terminal.until(b"\x1b[?5522h")
            sender.join(5)
            assert not sender.is_alive()
            os.write(fd, b"q")
            incoming2.until(b"q")
            assert b"status=DONE" not in incoming.drain()
            server.run("select-pane", "-t", pane)
            terminal.until(b"\x1b[?5522h")

            # Display dropped behind a protected protocol packet must redraw
            # when the output drains, without needing another display update.
            server.run("set-option", "-g", "status", "on")
            server.run("set-option", "-g", "status-interval", "0")
            terminal.drain()
            app.sendall(osc(b"type=write:id=redraw-pressure"))
            redraw_id = terminal.frame()[0][b"id"]
            pressure = b"A" * (512 * 1024)
            app.sendall(osc(b"type=wdata:mime=dGV4dC9wbGFpbg==", pressure))
            time.sleep(0.3)
            server.run("set-option", "-g", "status-left", "REDRAW-RECOVERED")
            server.run("set-option", "-g", "status-left-length", "30")
            time.sleep(0.1)
            assert terminal.frame()[1] == pressure
            terminal.until(b"REDRAW-RECOVERED")
            app.sendall(osc(b"type=wdata"))
            assert terminal.frame()[0][b"id"] == redraw_id
            os.write(fd, response(redraw_id, b"DONE", kind=b"write"))
            assert incoming.frame()[0][b"status"] == b"DONE"
            server.run("set-option", "-g", "status", "off")

            # >64MiB write: stream packets, do not accumulate the object in tmux.
            app.sendall(osc(b"type=write:id=large"))
            write_id = terminal.frame()[0][b"id"]
            data = base64.b64encode(bytes(range(256)) * 16)
            packet = osc(b"type=wdata:mime=YXBwbGljYXRpb24veC10ZXN0", data)
            count = 64 * 1024 * 1024 // 4096 + 1
            def produce():
                for _ in range(count):
                    app.sendall(packet)
                app.sendall(osc(b"type=wdata"))
            sender = threading.Thread(target=produce)
            sender.start()
            # Intentionally stop consuming briefly to exercise backpressure.
            time.sleep(0.3)
            digest = hashlib.sha256()
            for _ in range(count):
                fields, got = terminal.frame(timeout=15)
                assert fields[b"id"] == write_id and got == data
                digest.update(base64.b64decode(got))
            fields, got = terminal.frame()
            assert fields[b"type"] == b"wdata" and got is None
            sender.join(5)
            assert not sender.is_alive()
            os.write(fd, response(write_id, b"DONE", kind=b"write"))
            assert incoming.frame()[0][b"status"] == b"DONE"
            print("large write: %d decoded bytes, sha256=%s" %
                  (count * 4096, digest.hexdigest()))

            # Disabled mode still allows ordinary protocol queries; text fallback.
            app.sendall(b"\x1b[?5522l")
            terminal.until(b"\x1b[?5522l")
            os.write(fd, b"\x1b[200~text\npaste\x1b[201~")
            incoming.until(b"\x1b[200~text\npaste\x1b[201~")
            literal = (b"\x1b[200~" + osc(b"type=read:status=OK:id=literal") +
                       b"\x1b[?5522;2$y\x1b[201~")
            os.write(fd, literal)
            incoming.until(literal)

            # Invalid controls and overflowing native OSC cannot turn into a
            # valid prefix write; abort and drain through its terminator.
            malformed_packets = [
                osc(b"type=wdata:mime=dGV4dC9wbGFpbg==", bad)
                for bad in (b"Y\x01Q==", b"Y\x00Q==", b"YQ==\x1b",
                            b"YQ==\x1b\x07", b"A" * (1024 * 1024 + 1))]
            malformed_packets += [
                b"\x1b]" + prefix + b";type=wdata:mime=dGV4dC9wbGFpbg==;Y\x01Q==\x1b\\"
                for prefix in (b"05522", b"4294972818", b"55\x0022")]
            for bad_packet in malformed_packets:
                app.sendall(osc(b"type=write:id=bad-payload"))
                bad_id = terminal.frame()[0][b"id"]
                sender = threading.Thread(target=lambda: app.sendall(
                    bad_packet + osc(b"type=wdata")))
                sender.start()
                fields, payload = terminal.frame()
                assert fields[b"id"] == bad_id and payload == b"!"
                assert incoming.frame()[0][b"status"] == b"EINVAL"
                sender.join(5)
                assert not sender.is_alive()
                assert b"\x1b]5522;" not in terminal.quiet()

            # Sensitive wrapped passthrough must not reach debug/raw output logs.
            server.run("set-option", "-g", "allow-passthrough", "on")
            secret = osc(b"type=read:pw=" + token, b"cHJpdmF0ZS1wYXlsb2Fk")
            wrapped = b"\x1bPtmux;" + secret.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
            for byte in wrapped:
                app.sendall(bytes([byte]))
            terminal.until(b"cHJpdmF0ZS1wYXlsb2Fk")

            # A real nested server probes through the outer one and remaps twice.
            _, nested_app, nested_input = inner.pane(first=True)
            command = shlex.join(inner.args + [
                "attach-session", "-t", "clip", "-f", "clipboard-fence"])
            nested_pane = server.run("new-window", "-P", "-F", "#{pane_id}",
                                     command).strip().decode()
            time.sleep(0.3)
            nested_app.sendall(b"\x1b[?5522$p")
            nested_input.until(b"\x1b[?5522;2$y")
            nested_app.sendall(b"\x1b[?5522h")
            terminal.until(b"\x1b[?5522h")
            nested_app.sendall(osc(b"type=read:id=two-hops", b"Lg=="))
            ident = terminal.frame()[0][b"id"]
            assert ident != b"two-hops"
            os.write(fd, response(ident, b"OK") + response(ident, b"DONE"))
            assert nested_input.frame()[0][b"id"] == b"two-hops"
            assert nested_input.frame()[0][b"status"] == b"DONE"
            nested_detach = inner.command("detach-client", "-s", "clip")
            fence(fd, terminal)  # This fence is mapped through the outer hop.
            command_ok(nested_detach)
            server.run("kill-pane", "-t", nested_pane, check=False)

            # App-facing pane FIFO fence: keep reading through modeoff until a
            # full fresh reply, including the suffix of an already-started OSC.
            pane_fence = Server(binary, root, "pane-fence")
            try:
                _, a, inp = pane_fence.pane(first=True)
                pfd, pout = pane_fence.attach()
                a.sendall(b"\x1b[?5522h")
                pout.until(b"\x1b[?5522h")
                pfd2, pout2 = pane_fence.attach()
                pout2.until(b"\x1b[?5522h")
                grant = base64.b64encode(b"pane-fence-grant")
                os.write(pfd, osc(b"type=read:status=OK:pw=" + grant) +
                         osc(b"type=read:status=DONE"))
                inp.frame()
                inp.frame()
                a.sendall(osc(b"type=read:id=old-pane:pw=" + grant +
                             b":name=dGVzdA==", b"dGV4dC9wbGFpbg=="))
                old_id = pout.frame()[0][b"id"]
                # Deliberately oversized per-frame stress, not normative read
                # chunks (which carry at most 4096 decoded bytes).
                old_data = b"A" * (768 * 1024)
                write_all(pfd, response(old_id, b"OK") +
                          response(old_id, b"DATA", old_data, b"text/plain"))
                assert inp.frame()[0][b"status"] == b"OK"
                inp.until(b"\x1b]5522;")
                time.sleep(0.1)  # Socket/PTY queues retain the old frame suffix.
                partial_offer = osc(b"type=read:status=OK:pw=" + grant)
                os.write(pfd2, partial_offer[:-2])
                time.sleep(0.05)
                a.sendall(b"\x1b[?5522l" +
                          osc(b"type=read:id=pane-fresh-boundary", b"Lg=="))
                pout.until(b"\x1b[?5522l")
                pout2.until(b"\x1b[?5522l")
                # Ordinary metadata reads may select either eligible attachment.
                end = time.monotonic() + 5
                while not any(b"\x1b]5522;" in out.buffer for out in (pout, pout2)):
                    assert time.monotonic() < end
                    pout.read(0.01)
                    pout2.read(0.01)
                route_fd, route_out = next(
                    (f, out) for f, out in ((pfd, pout), (pfd2, pout2))
                    if b"\x1b]5522;" in out.buffer)
                fresh_id = route_out.frame()[0][b"id"]
                os.write(pfd2, partial_offer[-2:])
                # Complete stale mapped packets both before and after the fence.
                os.write(pfd, response(old_id, b"DONE"))
                sender = threading.Thread(target=lambda: write_all(
                    route_fd, response(fresh_id, b"OK") +
                    response(fresh_id, b"DATA", b"dGV4dC9wbGFpbg==", b".") +
                    response(fresh_id, b"DONE") + response(old_id, b"DONE")))
                sender.start()
                old_tail = inp.until(b"\x1b\\")[:-2]
                assert old_tail.partition(b";")[2] == old_data
                for status in (b"OK", b"DATA", b"DONE"):
                    fields, payload = inp.frame()
                    assert fields[b"id"] == b"pane-fresh-boundary"
                    assert fields[b"status"] == status
                    if status == b"DATA":
                        assert fields[b"mime"] == b"Lg==" and payload == b"dGV4dC9wbGFpbg=="
                sender.join(5)
                assert not sender.is_alive()
                os.write(pfd, response(old_id, b"DATA", b"bGF0ZQ==", b"text/plain"))
                os.write(pfd2, osc(b"type=read:status=OK:pw=" + grant) +
                         osc(b"type=read:status=DONE"))
                assert inp.drain() == b""
            finally:
                pane_fence.close()

            # Orderly detach completes a partially written OSC before reset.
            slow = Server(binary, root, "slow")
            try:
                _, a, slow_input = slow.pane(first=True)
                slow_fd, slow_out = slow.attach()
                a.sendall(b"\x1b[?5522$p")
                slow_input.until(b"\x1b[?5522;2$y")
                a.sendall(osc(b"type=write:id=slow"))
                slow_out.frame()
                data = b"A" * 100000
                a.sendall(osc(b"type=wdata:mime=dGV4dC9wbGFpbg==", data))
                slow_out.until(b"\x1b]5522;")
                time.sleep(0.1)
                pending = slow.command("detach-client", "-s", "clip")
                body = slow_out.until(b"\x1b\\")[:-2]
                assert body.partition(b";")[2] == data
                slow_out.until(b"\x1b[?5522l")
                fence(slow_fd, slow_out)
                command_ok(pending)
            finally:
                slow.close()

            # Lock and exec also drain a started output packet. Lock additionally
            # waits for a captured incoming response before external ownership.
            for action in ("lock", "exec"):
                handoff = Server(binary, root, action)
                try:
                    _, a, inp = handoff.pane(first=True)
                    hfd, hout = handoff.attach()
                    a.sendall(b"\x1b[?5522$p")
                    inp.until(b"\x1b[?5522;2$y")
                    marker = os.path.join(root, action + "-ran")
                    command = shlex.join([sys.executable, "-c",
                        "open(%r,'w').write('ran')" % marker])
                    if action == "lock":
                        handoff.run("set-option", "-g", "lock-command", command)
                    a.sendall(osc(b"type=write:id=handoff"))
                    hout.frame()
                    a.sendall(osc(b"type=wdata:mime=dGV4dC9wbGFpbg==", data))
                    hout.until(b"\x1b]5522;")
                    time.sleep(0.1)
                    if action == "lock":
                        pending = handoff.command("lock-client")
                    else:
                        pending = handoff.command(
                            "detach-client", "-s", "clip", "-E", command)
                    assert not os.path.exists(marker)
                    body = hout.until(b"\x1b\\")[:-2]
                    assert body.partition(b";")[2] == data
                    hout.until(b"\x1b[?5522l")
                    fence(hfd, hout)
                    command_ok(pending)
                    end = time.monotonic() + 5
                    while not os.path.exists(marker) and time.monotonic() < end:
                        time.sleep(0.01)
                    assert os.path.exists(marker)
                    if action == "lock":
                        hout.until(b"\x1b[?5522$p")
                        os.write(hfd, b"\x1b[?5522;2$y")
                        a.sendall(b"\x1b[?5522$p")
                        inp.until(b"\x1b[?5522;2$y")
                        os.unlink(marker)
                        a.sendall(osc(b"type=read:id=incoming-lock", b"Lg=="))
                        ident = hout.frame()[0][b"id"]
                        os.write(hfd, response(ident, b"OK"))
                        inp.frame()
                        partial = response(ident, b"DATA", b"YQ==", b"text/plain")
                        os.write(hfd, partial[:-4])
                        time.sleep(0.1)
                        pending = handoff.command("lock-client")
                        hout.until(b"\x1b[?5522l")
                        time.sleep(0.1)
                        assert not os.path.exists(marker)
                        os.write(hfd, partial[-4:])
                        fence(hfd, hout)
                        command_ok(pending)
                        hout.until(b"\x1b[?5522$p")
                        assert os.path.exists(marker)
                        os.write(hfd, b"\x1b[?5522;2$y")
                        os.write(hfd, b"j")
                        inp.until(b"j")
                        assert b"status=DATA" not in inp.drain()
                finally:
                    handoff.close()

            # Failure is bounded, explicit and isolated, never an alleged lock.
            for action in ("lock", "suspend", "detach", "exec"):
                isolated = Server(binary, root, "fail-" + action)
                monitor = None
                try:
                    _, a, inp = isolated.pane(first=True)
                    qfd, qout = isolated.attach()
                    a.sendall(b"\x1b[?5522$p")
                    inp.until(b"\x1b[?5522;2$y")
                    target = isolated.run("list-clients", "-F",
                                          "#{client_name}").strip().decode()
                    marker = os.path.join(root, "failed-" + action)
                    command = shlex.join([sys.executable, "-c",
                        "open(%r,'w').write('ran')" % marker])
                    isolated.run("set-option", "-g", "lock-command", command)
                    a.sendall(osc(b"type=read:id=old-in-transit", b"Lg=="))
                    old = qout.frame()[0][b"id"]
                    os.write(qfd, response(old, b"OK"))
                    inp.frame()
                    partial = response(old, b"DATA", b"YQ==", b"text/plain")
                    os.write(qfd, partial[:-4])
                    time.sleep(0.05)
                    other_fd, other_out = isolated.attach()
                    monitor = isolated.command("-C", "attach-session", "-t", "clip")
                    monitored = Stream(monitor.stdout.fileno())
                    monitored.until(b"%session-changed")
                    if action == "lock":
                        args = ["lock-client", "-t", target]
                    elif action == "suspend":
                        args = ["suspend-client", "-t", target]
                    elif action == "exec":
                        args = ["detach-client", "-t", target, "-E", command]
                    else:
                        args = ["detach-client", "-t", target]
                    started = time.monotonic()
                    pending = isolated.command(*args)
                    fields, payload = qout.frame()
                    assert payload == b"Lg==" and b"pw" not in fields
                    cut_id = fields[b"id"]
                    # Complete the actual old frame; it must never reach a pane.
                    os.write(qfd, partial[-4:])
                    if action == "suspend":
                        os.write(qfd, response(cut_id, b"EBUSY"))
                    elif action == "detach":
                        os.write(qfd, response(b"stale-fence", b"OK") +
                                 response(b"stale-fence", b"DATA", b"Lg==", b".") +
                                 response(b"stale-fence", b"DONE"))
                    elif action == "exec":
                        os.write(qfd, response(cut_id, b"DATA", b"Lg==", b".") +
                                 response(cut_id, b"DONE"))
                    stdout, stderr = pending.communicate(timeout=5)
                    assert pending.returncode != 0, (action, stdout, stderr)
                    assert b"NOT locked" in stderr and b"quarantined" in stderr
                    assert time.monotonic() - started < 4
                    assert not os.path.exists(marker)
                    monitored.until(b"%clipboard-handoff-failed " + target.encode())
                    states = isolated.run("list-clients", "-F",
                        "#{client_name} #{client_clipboard_state} #{client_flags}")
                    own = next(line for line in states.splitlines()
                               if line.startswith(target.encode() + b" "))
                    assert b"quarantined" in own and b"suspended" not in own
                    os.write(qfd, b"must-not-be-pane-input")
                    assert inp.quiet() == b""
                    os.write(other_fd, b"v")
                    inp.until(b"v")  # Another attachment and the pane still work.
                    retry = isolated.command(*args)
                    _, error = retry.communicate(timeout=1)
                    assert retry.returncode != 0 and b"quarantined" in error
                    if action == "lock":
                        isolated.run("refresh-client", "-t", target,
                                     "-f", "!clipboard-fence")
                        isolated.run("refresh-client", "-t", target,
                                     "-f", "clipboard-fence")
                        states = isolated.run("list-clients", "-F",
                            "#{client_name} #{client_clipboard_state}")
                        assert target.encode() + b" quarantined" in states
                        # Late full proof changes state only. It does not lock.
                        os.write(qfd, response(cut_id, b"OK") +
                                 response(cut_id, b"DATA", b"Lg==", b".") +
                                 response(cut_id, b"DONE"))
                        time.sleep(0.1)
                        assert not os.path.exists(marker)
                        states = isolated.run("list-clients", "-F",
                            "#{client_name} #{client_clipboard_state}")
                        assert target.encode() + b" quarantined-ready" in states
                        retry = isolated.command(*args)
                        command_ok(retry)
                        qout.until(b"\x1b[?5522$p")
                        assert os.path.exists(marker)
                        os.write(qfd, b"\x1b[?5522;2$y")
                finally:
                    if monitor is not None:
                        monitor.terminate()
                        monitor.communicate(timeout=5)
                    isolated.close()

            # Default-off opt-in gate, explicit enable/removal/re-enable.
            gated = Server(binary, root, "gated")
            try:
                _, a, inp = gated.pane(first=True)
                gfd, gout = gated.attach(verified=False)
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;0$y")
                a.sendall(osc(b"type=read:id=not-opted-in", b"Lg=="))
                assert inp.frame()[0][b"status"] == b"ENOSYS"
                target = gated.run("list-clients", "-F",
                                   "#{client_name}").strip().decode()
                gated.run("refresh-client", "-t", target, "-f", "clipboard-fence")
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;2$y")
                a.sendall(osc(b"type=write:id=remove-ordinary-write"))
                ident = gout.frame()[0][b"id"]
                gated.run("refresh-client", "-t", target, "-f", "!clipboard-fence")
                gout.until(b"\x1b[?5522l")
                os.write(gfd, response(ident, b"DONE", kind=b"write"))
                assert inp.quiet() == b""
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;0$y")
                gated.run("refresh-client", "-t", target, "-f", "clipboard-fence")
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;2$y")
            finally:
                gated.close()

            # Both an unsupported probe and a late positive reply stay unsupported.
            unsupported = Server(binary, root, "unsupported")
            try:
                _, a, inp = unsupported.pane(first=True)
                late_fd, _ = unsupported.attach(support=None)
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;0$y")
                os.write(late_fd, b"\x1b[?5522;2$y")
                a.sendall(b"\x1b[?5522$p")
                inp.until(b"\x1b[?5522;0$y")
                assert inp.quiet() == b""
            finally:
                unsupported.close()
        finally:
            inner.close()
            server.close()
        for name in os.listdir(root):
            if name.endswith(".log"):
                with open(os.path.join(root, name), "rb") as log:
                    content = log.read()
                assert token not in content, name
                assert b"cHJpdmF0ZS1wYXlsb2Fk" not in content, name
        print("OSC5522 mediation tests passed")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--helper"]:
        helper(sys.argv[2])
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "./tmux")
