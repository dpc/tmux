#!/usr/bin/env python3
"""Opaque file-export routing; private servers, no desktop clipboard access."""

import base64
import hashlib
import os
import shlex
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
from clipboard import Server, osc, response


MIME = base64.b64encode(b"application/vnd.tau.file-export-v1")
SECRET = b"file-export-private-payload"


def opener(ident=b"file-original"):
    return osc(b"type=write-file:id=" + ident)


def chunk(ident=b"file-original", payload=None):
    return osc(b"type=write-file-data:id=" + ident + b":mime=" + MIME,
               base64.b64encode(SECRET) if payload is None else payload)


def commit(ident=b"file-original"):
    return osc(b"type=write-file-data:id=" + ident)


def reply(fd, ident, status=b"DONE"):
    os.write(fd, response(ident, status, kind=b"write-file"))


def start(server):
    pane, app, incoming = server.pane(first=True)
    fd, terminal = server.attach(verified=False)
    app.sendall(b"\x1b[?5522$p")
    incoming.until(b"\x1b[?5522;2$y")
    app.sendall(b"\x1b[?5522h")
    terminal.until(b"\x1b[?5522h")
    return pane, app, incoming, fd, terminal


def opened(app, terminal, ident=b"file-original"):
    app.sendall(opener(ident))
    fields, payload = terminal.frame()
    assert fields[b"type"] == b"write-file" and payload is None
    assert fields[b"id"] != ident
    return fields[b"id"]


def completed(incoming, ident=b"file-original", status=b"DONE"):
    fields, payload = incoming.frame()
    assert fields == {b"type": b"write-file", b"id": ident, b"status": status}
    assert payload is None


def no_protocol(terminal):
    assert b"\x1b]5522;" not in terminal.drain()


def transfer(app, incoming, fd, terminal, ident=b"file-original"):
    mapped = opened(app, terminal, ident)
    app.sendall(chunk(ident) + commit(ident))
    fields, payload = terminal.frame()
    assert fields == {b"type": b"write-file-data", b"id": mapped, b"mime": MIME}
    assert payload == base64.b64encode(SECRET)
    fields, payload = terminal.frame()
    assert fields == {b"type": b"write-file-data", b"id": mapped}
    assert payload is None
    assert incoming.quiet() == b""  # No admission or synthetic completion.
    reply(fd, mapped)
    completed(incoming, ident)
    return mapped


def single(binary, root):
    server = Server(binary, root, "files", native=True)
    try:
        pane, app, incoming, fd, terminal = start(server)
        stale = transfer(app, incoming, fd, terminal)

        # The mux must not interpret MIME or payload, even as a file bundle.
        mapped = opened(app, terminal)
        app.sendall(osc(b"type=write-file-data:id=file-original:mime=opaque",
                        b"not-base64-not-a-bundle") + commit())
        fields, payload = terminal.frame()
        assert fields[b"mime"] == b"opaque" and payload == b"not-base64-not-a-bundle"
        terminal.frame()
        reply(fd, mapped, b"EINVAL")  # Validation belongs to the terminal.
        completed(incoming, status=b"EINVAL")

        # Raw private MIME stays raw. Neither valid nor malformed file packets
        # can append, commit or poison a pending raw write.
        app.sendall(osc(b"type=write:id=raw"))
        raw_id = terminal.frame()[0][b"id"]
        app.sendall(opener())
        completed(incoming, status=b"EBUSY")
        app.sendall(chunk() + commit() +
                    osc(b"type=write-file-data:id=x:id=y", b"broken") +
                    chunk(payload=b"Y\x01Q=="))
        no_protocol(terminal)
        app.sendall(osc(b"type=wdata:mime=" + MIME, base64.b64encode(SECRET)) +
                    osc(b"type=wdata"))
        fields, payload = terminal.frame()
        assert fields[b"type"] == b"wdata" and fields[b"id"] == raw_id
        assert payload == base64.b64encode(SECRET)
        assert terminal.frame()[0][b"type"] == b"wdata"
        os.write(fd, response(raw_id, b"DONE", kind=b"write"))
        assert incoming.frame()[0][b"id"] == b"raw"

        # File writes reject raw openers, ignore raw chunks/aliases (including
        # malformed frames), and retain ownership through asynchronous DONE.
        mapped = opened(app, terminal)
        app.sendall(osc(b"type=write:id=raw-busy"))
        assert incoming.frame()[0][b"status"] == b"EBUSY"
        app.sendall(osc(b"type=wdata", b"raw") + osc(b"type=walias", b"alias") +
                    osc(b"type=wdata") +
                    osc(b"type=wdata:mime=x:mime=y", b"bad") +
                    osc(b"type=wdata", b"Y\x00Q=="))
        no_protocol(terminal)
        app.sendall(commit())
        assert terminal.frame()[0][b"id"] == mapped
        app.sendall(opener(b"second") + osc(b"type=read:id=busy", b"Lg=="))
        completed(incoming, b"second", b"EBUSY")
        assert incoming.frame()[0][b"status"] == b"EBUSY"
        reply(fd, stale)
        assert incoming.quiet() == b""
        reply(fd, mapped)
        completed(incoming)

        # Closed metadata schema and canonical IDs. Invalid openers never route.
        bad_openers = [
            b"type=write-file", b"type=write-file:id=",
            b"type=write-file:id=unsafe!", b"type=write-file:id=x:id=x",
            b"type=write-file:type=write-file:id=x",
        ] + [b"type=write-file:id=x:" + field for field in (
            b"loc=", b"loc=clipboard", b"pw=x", b"name=x", b"mime=x",
            b"status=DONE", b"unknown=x", b"mode=file", b"size=1")]
        for meta in bad_openers:
            app.sendall(osc(meta))
            fields, _ = incoming.frame()
            assert fields[b"type"] == b"write-file" and fields[b"status"] == b"EINVAL"
            no_protocol(terminal)
        app.sendall(osc(b"type=write-file:id=x", b""))
        assert incoming.frame()[0][b"status"] == b"EINVAL"
        no_protocol(terminal)

        # Every continuation must carry the exact original canonical ID.
        bad_data = [
            osc(b"type=write-file-data"),
            commit(b""), commit(b"wrong"), commit(b"file-original!"),
            commit(stale),
            osc(b"type=write-file-data:id=file-original:id=file-original"),
            osc(b"type=write-file-data:id=file-original:unknown=x"),
            osc(b"type=write-file-data:id=file-original:loc="),
            osc(b"type=write-file-data:id=file-original:status=DONE"),
            osc(b"type=write-file-data:id=file-original:mime=x:mime=y", b"YQ=="),
            osc(b"type=write-file-data:id=file-original:mime=" + MIME),
            osc(b"type=write-file-data:id=file-original", b""),
            chunk(payload=b"Y\x01Q=="),
            # No recognizable family: preserve fail-closed framing behavior.
            osc(b"type=write-file-dat:id=file-original:broken"),
            osc(b"type=read:id=file-original:broken"),
            osc(b"id=file-original:broken"),
        ]
        for packet in bad_data:
            mapped = opened(app, terminal)
            app.sendall(packet + commit())
            fields, payload = terminal.frame()
            assert fields[b"type"] == b"write-file-data" and fields[b"id"] == mapped
            assert payload == b"!"  # File-specific poison, never raw wdata.
            completed(incoming, status=b"EINVAL")
            no_protocol(terminal)
            reply(fd, mapped, b"EINVAL")
            assert incoming.quiet() == b""

        # Errors retain their family and release ownership, before/after commit.
        for status in (b"EPERM", b"ENOSYS", b"EBUSY", b"EINVAL", b"EFBIG", b"EIO"):
            mapped = opened(app, terminal)
            reply(fd, mapped, status)
            completed(incoming, status=status)
        mapped = opened(app, terminal)
        app.sendall(commit())
        terminal.frame()
        reply(fd, mapped, b"EIO")
        completed(incoming, status=b"EIO")

        # Premature DONE, a raw reply, OK admission, and extra response fields
        # cannot prove success. Each cancels its generation and swallows late ACK.
        for status, kind, extra in (
                (b"DONE", b"write-file", b""),
                (b"DONE", b"write", b""),
                (b"OK", b"write-file", b""),
                (b"EIO", b"write-file", b":unknown=x")):
            mapped = opened(app, terminal)
            os.write(fd, osc(b"type=" + kind + b":id=" + mapped +
                             b":status=" + status + extra))
            terminal.until(b"\x1b[?5522l")
            reply(fd, mapped)
            assert incoming.quiet() == b""

        # Extra postcommit packets need generation cancellation, not poison:
        # the terminal may have already admitted asynchronous publication.
        mapped = opened(app, terminal)
        app.sendall(commit())
        terminal.frame()
        app.sendall(chunk())
        terminal.until(b"\x1b[?5522l")
        completed(incoming, status=b"EINVAL")
        reply(fd, mapped)
        assert incoming.quiet() == b""

        # Client pinning: a later attachment may not receive continuations.
        mapped = opened(app, terminal)
        fd2, terminal2 = server.attach(verified=False)
        terminal2.drain()
        app.sendall(chunk() + commit())
        assert terminal.frame()[0][b"id"] == mapped
        assert terminal.frame()[0][b"id"] == mapped
        no_protocol(terminal2)
        reply(fd, mapped)
        completed(incoming)
        # Keep both clients attached; select the first via activity.
        os.write(fd, b"z")
        incoming.until(b"z")

        # Focus/mode revocation after commit must suppress the late DONE.
        mapped = opened(app, terminal)
        app.sendall(commit())
        terminal.frame()
        app.sendall(b"\x1b[?5522l")
        terminal.until(b"\x1b[?5522l")
        reply(fd, mapped)
        assert incoming.quiet() == b""
        app.sendall(b"\x1b[?5522h")
        terminal.until(b"\x1b[?5522h")

        # Slow terminal: backpressure remains bounded, every opaque byte survives.
        mapped = opened(app, terminal)
        data = base64.b64encode(bytes(range(256)) * 16)
        count = 400
        sender = threading.Thread(target=lambda: app.sendall(
            chunk(payload=data) * count + commit()))
        sender.start()
        sender.join(0.2)
        assert sender.is_alive(), "producer did not encounter backpressure"
        digest = hashlib.sha256()
        for _ in range(count):
            fields, payload = terminal.frame()
            assert fields[b"id"] == mapped and fields[b"mime"] == MIME
            digest.update(payload)
        assert digest.digest() == hashlib.sha256(data * count).digest()
        assert terminal.frame()[0][b"id"] == mapped
        sender.join(5)
        assert not sender.is_alive()
        reply(fd, mapped)
        completed(incoming)

        # Background panes contend rather than steal the selected attachment.
        _, other, other_input = server.pane()
        mapped = opened(app, terminal)
        other.sendall(opener(b"other"))
        completed(other_input, b"other", b"EBUSY")
        reply(fd, mapped, b"EPERM")
        completed(incoming, status=b"EPERM")

        # An old peer can silently ignore this family. The mux deadline must
        # produce a file error, cancel admitted work and forget a late ACK.
        mapped = opened(app, terminal)
        app.sendall(commit())
        terminal.frame()
        fields, _ = incoming.frame(timeout=35)
        assert fields == {b"type": b"write-file", b"id": b"file-original",
                          b"status": b"EIO"}
        terminal.until(b"\x1b[?5522l")
        reply(fd, mapped)
        assert incoming.quiet() == b""
    finally:
        server.close()


def nested(binary, root, old_binary=None, old_outer=False):
    tag = "nested" if old_binary is None else ("old-outer" if old_outer else "old-inner")
    outer = Server(old_binary if old_outer else binary, root, tag + "-outer", native=True)
    inner = Server(old_binary if old_binary and not old_outer else binary,
                   root, tag + "-inner", native=True)
    try:
        _, _, _, fd, terminal = start(outer)
        _, app, incoming = inner.pane(first=True)
        command = shlex.join(inner.args + ["attach-session", "-t", "clip"])
        outer.run("new-window", command)
        time.sleep(0.3)  # Let the nested attachment complete its capability probe.
        app.sendall(b"\x1b[?5522$p")
        incoming.until(b"\x1b[?5522;2$y")
        app.sendall(b"\x1b[?5522h")
        terminal.until(b"\x1b[?5522h")
        if old_binary is None:
            transfer(app, incoming, fd, terminal, b"nested-original")
            return

        # The actual old hop has a pending same-pane raw mapping. Neither a
        # new hop inside nor outside it may turn file bytes/commit into wdata.
        app.sendall(osc(b"type=write:id=prior-raw"))
        raw_id = terminal.frame()[0][b"id"]
        app.sendall(opener() + chunk() + commit())
        no_protocol(terminal)
        if not old_outer:
            assert incoming.quiet() == b""  # Old inner drops entire family.
        else:
            completed(incoming, status=b"EBUSY")  # New inner owns raw mapping.
        app.sendall(osc(b"type=wdata:mime=" + MIME, b"cmF3") + osc(b"type=wdata"))
        fields, payload = terminal.frame()
        assert fields[b"type"] == b"wdata" and fields[b"id"] == raw_id
        assert payload == b"cmF3"
        assert terminal.frame()[0][b"type"] == b"wdata"
        os.write(fd, response(raw_id, b"DONE", kind=b"write"))
        assert incoming.frame()[0][b"id"] == b"prior-raw"
        if old_outer:
            # Bypass only the inner mediator to establish an outer-only raw
            # mapping. Now the upgraded inner really forwards file packets to
            # the old outer while that raw mapping is live.
            inner.run("set-option", "-g", "allow-passthrough", "on")

            def passthrough(packet):
                return b"\x1bPtmux;" + packet.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"

            app.sendall(passthrough(osc(b"type=write:id=outer-only")))
            outer_raw = terminal.frame()[0][b"id"]
            app.sendall(opener(b"unsupported") + chunk(b"unsupported") +
                        commit(b"unsupported"))
            no_protocol(terminal)
            assert incoming.quiet() == b""
            app.sendall(passthrough(osc(b"type=wdata:mime=" + MIME, b"cmF3")) +
                        passthrough(osc(b"type=wdata")))
            fields, payload = terminal.frame()
            assert fields[b"type"] == b"wdata" and fields[b"id"] == outer_raw
            assert payload == b"cmF3"
            assert terminal.frame()[0][b"type"] == b"wdata"
            os.write(fd, response(outer_raw, b"DONE", kind=b"write"))
            assert incoming.quiet() == b""  # Not the new inner's file reply.
            return
        # With no prior mapping, the old hop still drops the whole family.
        app.sendall(opener(b"unsupported") + chunk(b"unsupported") + commit(b"unsupported"))
        no_protocol(terminal)
        assert incoming.quiet() == b""
    finally:
        inner.close()
        outer.close()


def main(binary, old_binary=None):
    binary = os.path.abspath(binary)
    old_binary = os.path.abspath(old_binary) if old_binary else None
    with tempfile.TemporaryDirectory(prefix="tmux-clipboard-file-") as root:
        single(binary, root)
        nested(binary, root)
        if old_binary:
            nested(binary, root, old_binary, old_outer=False)
            nested(binary, root, old_binary, old_outer=True)
        for name in os.listdir(root):
            if name.endswith(".log"):
                with open(os.path.join(root, name), "rb") as log:
                    content = log.read()
                assert SECRET not in content and base64.b64encode(SECRET) not in content
    print("OSC5522 file transport tests passed" +
          (" (including old-hop compatibility)" if old_binary else ""))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./tmux",
         sys.argv[2] if len(sys.argv) > 2 else None)
