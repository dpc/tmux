#!/usr/bin/env python3
"""Private native cell-border integration fixture; no real terminal is involved."""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unicodedata

ESC = b"\x1b"
ST = ESC + b"\\"
PREFIX = ESC + b"P1+bcell-border;"
BASE = "pen,rect,half,center,palette"
FULL = BASE + ",style,round,priority,outside,radius"
WIDTH, HEIGHT = 80, 24


def dcs(body):
    return PREFIX + body.encode() + ST


def helper(path):
    """App PTY transport, following regress/clipboard.py's socket bridge."""
    tty.setraw(0)
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(path)
    sock.sendall(b"READY")
    while True:
        for source in select.select([0, sock], [], [])[0]:
            data = os.read(0, 65536) if source == 0 else sock.recv(65536)
            if not data:
                return
            if source == 0:
                sock.sendall(data)
            else:
                while data:
                    data = data[os.write(1, data):]


def blank():
    return {"segments": {}, "round": 0, "outside": None, "radius": None}


def clone(value):
    return dict(value, segments=dict(value["segments"]))


class Terminal:
    """Observe wire-level border metadata, not pixels or rendered appearance.

    Interpret cursor-addressed output and the proposed DCS operations so tests
    allow either pen-based or rectangle-based tmux serialization.
    """
    def __init__(self, fd, features=FULL, respond=True):
        self.fd, self.features, self.respond = fd, features, respond
        self.lock = threading.RLock()
        self.pending = b""
        self.trace = bytearray()
        self.frames = []
        self.pen = blank()
        self.cells = {}
        self.row = self.col = 0
        self.saved = (0, 0, blank())
        self.queries = 0
        self.running = True
        self.failure = None
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        try:
            while self.running:
                if not select.select([self.fd], [], [], .03)[0]:
                    continue
                data = os.read(self.fd, 65536)
                if not data:
                    break
                with self.lock:
                    self.trace.extend(data)
                    self.pending += data
                    self.parse()
        except OSError as error:
            if self.running and error.errno != errno.EIO:
                self.failure = error
        except BaseException as error:
            self.failure = error

    def parse(self):
        while self.pending:
            data = self.pending
            if data[0] == 27:
                if len(data) < 2:
                    return
                if data[1:2] in (b"P", b"]", b"_", b"^"):
                    end = data.find(ST, 2)
                    bel = data.find(b"\x07", 2) if data[1:2] == b"]" else -1
                    if bel >= 0 and (end < 0 or bel < end):
                        end, size = bel, 1
                    else:
                        size = 2
                    if end < 0:
                        return
                    frame = data[:end + size]
                    self.pending = data[end + size:]
                    if frame.startswith(PREFIX):
                        body = frame[len(PREFIX):-2].decode("ascii")
                        self.frames.append(body)
                        if body == "q":
                            self.queries += 1
                            if self.respond and self.features is not None:
                                os.write(self.fd, dcs("ok;1;" + self.features))
                        else:
                            self.border(body)
                    continue
                if data[1:2] == b"[":
                    match = re.match(rb"\x1b\[([0-?]*)([ -/]*)([@-~])", data)
                    if not match:
                        return
                    self.pending = data[match.end():]
                    self.csi(*match.groups())
                    continue
                if data[1:2] in (b"(", b")", b"*", b"+", b"%", b"#"):
                    if len(data) < 3:
                        return
                    self.pending = data[3:]
                    continue
                self.pending = data[2:]
                if data[1:2] == b"7":
                    self.saved = self.row, self.col, clone(self.pen)
                elif data[1:2] == b"8":
                    self.row, self.col, self.pen = self.saved
                    self.pen = clone(self.pen)
                elif data[1:2] == b"c":
                    self.cells.clear()
                    self.pen = blank()
                    self.row = self.col = 0
                continue
            if data[0] >= 128:
                length = (2 if data[0] < 224 else 3 if data[0] < 240 else 4)
                if len(data) < length:
                    return
                try:
                    char = data[:length].decode("utf-8")
                except UnicodeDecodeError:
                    self.pending = data[1:]
                    continue
                self.pending = data[length:]
                width = (0 if unicodedata.combining(char) else
                         2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
                for col in range(self.col, min(WIDTH, self.col + width)):
                    self.paint(self.row, col)
                self.col = min(WIDTH - 1, self.col + width)
                continue
            self.pending = data[1:]
            byte = data[0]
            if byte == 13:
                self.col = 0
            elif byte == 10:
                self.row = min(HEIGHT - 1, self.row + 1)
            elif byte == 8:
                self.col = max(0, self.col - 1)
            elif byte == 9:
                self.col = min(WIDTH - 1, (self.col // 8 + 1) * 8)
            elif 32 <= byte < 127:
                self.paint(self.row, self.col)
                self.col = min(WIDTH - 1, self.col + 1)

    def paint(self, row, col):
        self.cells[row, col] = clone(self.pen)

    def csi(self, params, intermediate, final):
        private = params.startswith((b"?", b">", b"="))
        nums = [int(x) if x.isdigit() else 0
                for x in params.lstrip(b"?=>").split(b";")]
        n = nums[0] or 1
        if final == b"c":
            os.write(self.fd, b"\x1b[>0;1;0c" if params.startswith(b">")
                     else b"\x1b[?1;2c")
        elif final in (b"H", b"f"):
            self.row = max(0, nums[0] - 1)
            self.col = max(0, (nums[1] if len(nums) > 1 else 1) - 1)
        elif final == b"G":
            self.col = n - 1
        elif final == b"d":
            self.row = n - 1
        elif final == b"A":
            self.row = max(0, self.row - n)
        elif final == b"B":
            self.row = min(HEIGHT - 1, self.row + n)
        elif final == b"C":
            self.col = min(WIDTH - 1, self.col + n)
        elif final == b"D":
            self.col = max(0, self.col - n)
        elif final == b"m" and not private and 0 in nums:
            self.pen = blank()
        elif final == b"X":
            for col in range(self.col, min(WIDTH, self.col + n)):
                self.paint(self.row, col)
        elif final in (b"J", b"K"):
            for row in range(HEIGHT):
                for col in range(WIDTH):
                    if final == b"K" and row != self.row:
                        continue
                    position, here = (row, col), (self.row, self.col)
                    if nums[0] == 2 or (nums[0] == 0 and position >= here) or (
                            nums[0] == 1 and position <= here):
                        self.paint(row, col)

    def border(self, body):
        fields = body.split(";")
        op = fields.pop(0)
        if op in ("ok",):
            return
        mask = int(fields.pop(0))
        stroke = None
        if op in ("pen", "set"):
            stroke = tuple(fields[:4])
            fields = fields[4:]
        if op in ("set", "clear", "round", "square"):
            rows, cols = map(int, fields[:2])
            fields = fields[2:]
            targets = [self.cells.setdefault((row, col), blank())
                       for row in range(self.row, min(HEIGHT, self.row + rows))
                       for col in range(self.col, min(WIDTH, self.col + cols))]
        else:
            targets = [self.pen]
        extensions = dict(field.split("=", 1) for field in fields)
        for cell in targets:
            if op in ("pen", "set", "pen-clear", "clear"):
                for bit in range(12):
                    if mask & (1 << bit):
                        if stroke is None:
                            cell["segments"].pop(bit, None)
                        else:
                            cell["segments"][bit] = (
                                stroke, extensions.get("style", "solid"),
                                extensions.get("priority", "0"))
            else:
                cell["round"] = (cell["round"] | mask if op in (
                    "pen-round", "round") else cell["round"] & ~mask)
                for key in ("outside", "radius"):
                    if key in extensions:
                        cell[key] = extensions[key]

    def snapshot(self):
        time.sleep(.2)
        with self.lock:
            if self.failure:
                raise self.failure
            return {key: clone(value) for key, value in self.cells.items()}

    def wait_query(self, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.queries:
                time.sleep(.1)
                return
            time.sleep(.02)
        raise AssertionError("native capability negotiation query not emitted")

    def close(self):
        self.running = False
        self.thread.join(timeout=2)


class Server:
    def __init__(self, binary, root, options, capture=None, provenance=None):
        self.binary, self.root = binary, root
        self.capture = capture
        self.app_output = bytearray()
        self.app_input = bytearray()
        self.metadata = dict(provenance or {}, initial_terminal={
            "cols": WIDTH, "rows": HEIGHT}, status="off", geometry=[])
        config = os.path.join(root, "tmux.conf")
        with open(config, "w") as file:
            file.write("set -g status off\nset -g default-terminal xterm-256color\n")
            file.write("set -g window-size manual\n")
            for option in options:
                file.write(option + "\n")
        self.args = [binary, "-S", os.path.join(root, "socket"), "-f", config]
        self.clients, self.apps = [], []

    def run(self, *args, check=True):
        result = subprocess.run(self.args + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.root)
        if check and result.returncode:
            raise AssertionError((args, result.stderr.decode(errors="replace")))
        if self.capture and not result.returncode and args[0] in (
                "new-session", "split-window", "resize-window", "resize-pane"):
            self.geometry(args[0])
        return result.stdout.strip()

    def geometry(self, event):
        fields = ("session_name", "window_id", "pane_id", "pane_left", "pane_top",
                  "pane_width", "pane_height", "cursor_x", "cursor_y",
                  "history_size")
        result = subprocess.run(self.args + [
            "list-panes", "-a", "-F", "|".join("#{%s}" % field for field in fields)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.root)
        panes = []
        for line in result.stdout.decode().splitlines():
            values = line.split("|")
            panes.append(dict(zip(fields, values[:3] + [
                int(value) for value in values[3:]])))
        self.metadata["geometry"].append({"event": event, "panes": panes})

    def pane(self, first=False):
        path = os.path.join(self.root, "app-%d" % len(self.apps))
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen()
        listener.settimeout(5)
        command = shlex.join([sys.executable, os.path.abspath(__file__),
                              "--helper", path])
        args = ["new-session", "-d", "-s", "border", "-x", str(WIDTH),
                "-y", str(HEIGHT)] if first else ["split-window", "-h", "-d"]
        pane = self.run(*args, "-P", "-F", "#{pane_id}", command).decode()
        app, _ = listener.accept()
        listener.close()
        app.settimeout(3)
        assert app.recv(5) == b"READY"
        self.apps.append(app)
        return pane, App(app, self)

    def attach(self, features=FULL, respond=True, verify=True, session="border"):
        fd, slave = os.openpty()
        fcntl.ioctl(fd, 0x5414, struct.pack("HHHH", HEIGHT, WIDTH, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        process = subprocess.Popen(
            self.args + ["attach-session", "-t", session],
            stdin=slave, stdout=slave, stderr=slave, env=env, cwd=self.root,
            start_new_session=True)
        os.close(slave)
        terminal = Terminal(fd, features, respond)
        self.clients.append((process, fd, terminal))
        if verify:
            terminal.wait_query()
        return terminal

    def query(self, app):
        app.sendall(dcs("q") + b"\x1b[c")
        incoming = b""
        until = time.monotonic() + 3
        while not re.search(rb"\x1b\[\?[0-9;]*c", incoming):
            if time.monotonic() > until:
                raise AssertionError("pane DA1 barrier missing")
            incoming += app.recv(65536)
        return re.findall(rb"\x1bP1\+bcell-border;ok;1;([^\x1b]*)\x1b\\",
                          incoming)

    def sync(self, app, terminal):
        self.query(app)
        self.run("refresh-client")
        if self.capture:
            self.geometry("refresh")
        return terminal.snapshot()

    def close(self):
        # Replay the observed test, not tmux's later detach/terminal teardown.
        captured_outer = []
        for _, _, terminal in self.clients:
            with terminal.lock:
                captured_outer.append(bytes(terminal.trace))
        self.run("kill-server", check=False)
        for app in self.apps:
            app.close()
        for process, fd, terminal in self.clients:
            terminal.close()
            os.close(fd)
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        if self.capture:
            os.makedirs(self.capture, exist_ok=True)
            with open(os.path.join(self.capture, "metadata.json"), "w") as file:
                json.dump(self.metadata, file, indent=2, sort_keys=True)
                file.write("\n")
            for name, data in [("apps.bin", self.app_output),
                               ("app-input.bin", self.app_input)] + [
                    ("outer.bin" if index == 0 else "outer-%d.bin" % index,
                     data)
                    for index, data in enumerate(captured_outer)]:
                with open(os.path.join(self.capture, name), "wb") as file:
                    file.write(data)


class App:
    def __init__(self, sock, server):
        self.sock, self.server = sock, server

    def sendall(self, data):
        self.server.app_output.extend(data)
        self.sock.sendall(data)

    def recv(self, size):
        data = self.sock.recv(size)
        self.server.app_input.extend(data)
        return data


def segments(screen, row, col):
    return screen.get((row, col), blank())["segments"]


def has(screen, row, col, mask, color):
    actual = segments(screen, row, col)
    for bit in range(12):
        if mask & (1 << bit):
            assert bit in actual and actual[bit][0][1].lower() == color.lower(), (
                "missing border", row, col, bit, color, actual)


def empty(screen, row, col, stage=None):
    assert not segments(screen, row, col), ("unexpected border", stage, row, col,
                                          segments(screen, row, col))


def negotiation(server):
    _, app = server.pane(first=True)
    assert server.query(app) == [], "detached pane advertised support"
    terminal = server.attach(respond=False)
    assert server.query(app) == [], "unverified terminal advertised support"
    os.write(terminal.fd, dcs("ok;1;" + FULL))
    time.sleep(.15)
    replies = server.query(app)
    assert len(replies) == 1 and set(replies[0].decode().split(",")) == set(FULL.split(",")), replies
    server.attach(features=BASE)
    replies = server.query(app)
    assert len(replies) == 1 and set(replies[0].decode().split(",")) == set(BASE.split(",")), replies
    server.run("new-session", "-d", "-s", "unrelated",
               shlex.join([sys.executable, "-c", "import time; time.sleep(60)"]))
    server.attach(features=None, session="unrelated")
    replies = server.query(app)
    assert len(replies) == 1 and set(replies[0].decode().split(",")) == set(BASE.split(",")), (
        "unrelated client vetoed capabilities", replies)
    server.attach(features=None)
    assert server.query(app) == [], "unsupported relevant client not included"


def state(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    assert server.query(app), "verified client did not enable native support"
    app.sendall(b"\x1b[2J\x1b[H" + dcs("pen;255;8;112233;0;1") + b"A"
                + b"\x1b7" + dcs("pen-clear;4095") + b"B\x1b8\x1b[1;3HC"
                + b"\x1b[0mD\x1b[2;1H" + dcs("set;3;9;445566;1;0;2;3"))
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 255, "112233")
    empty(screen, 0, 1)
    has(screen, 0, 2, 255, "112233")
    empty(screen, 0, 3)
    for row in (1, 2):
        for col in (0, 1, 2):
            has(screen, row, col, 3, "445566")
    empty(screen, 1, 3)
    # A malformed frame must not mutate even its valid prefix.
    app.sendall(b"\x1b[2;1H" + dcs("set;3;9;ffffff;1;0;2;3;BAD=value")
                + dcs("clear;3;2;3;extra")
                + dcs("set;3;65;ffffff;0;0;2;3")
                + dcs("pen;255;8;ffffff;0;0;bad=") + b"\x1b[5;1HX")
    screen = server.sync(app, terminal)
    has(screen, 1, 0, 3, "445566")
    empty(screen, 4, 0)
    # Erasure takes the pen; rectangular mutation does not change the pen.
    app.sendall(b"\x1b[4;1H" + dcs("pen;192;12;p7;0;0") + b"\x1b[3X"
                + dcs("set;3;8;abcdef;0;0;1;1") + b"\x1b[5;1HZ")
    screen = server.sync(app, terminal)
    for col in (0, 1, 2):
        has(screen, 3, col, 192, "p7")
    has(screen, 4, 0, 192, "p7")
    assert 0 not in segments(screen, 4, 0), "rectangle changed pen"
    with terminal.lock:
        assert b"\x1bPtmux;" not in terminal.trace, "passthrough framing emitted"


def offsets(server):
    _, _ = server.pane(first=True)
    pane, app = server.pane()
    terminal = server.attach()
    left, top, width, height = map(int, server.run(
        "display-message", "-p", "-t", pane,
        "#{pane_left} #{pane_top} #{pane_width} #{pane_height}").split())
    assert left > 0
    app.sendall(b"\x1b[2;3H" + dcs("set;4095;7;123abc;0;0;2;3"))
    screen = server.sync(app, terminal)
    for row in (top + 1, top + 2):
        for col in (left + 2, left + 3, left + 4):
            has(screen, row, col, 4095, "123abc")
    empty(screen, top + 1, 2)
    # Oversized rectangle must clip to pane, not overwrite its neighbour.
    app.sendall(b"\x1b[%d;%dH" % (height, width)
                + dcs("set;255;8;eeeeee;0;0;65535;65535"))
    screen = server.sync(app, terminal)
    has(screen, top + height - 1, left + width - 1, 255, "eeeeee")
    empty(screen, top + height - 1, left - 1)


def movement(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[2J\x1b[H" + dcs("pen;3;8;102030;0;0") + b"A"
                + b"\x1b[0m\x1b[2;1H" + dcs("pen;12;8;405060;0;0") + b"B"
                + b"\x1b[0m\x1b[1S")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 12, "405060")
    empty(screen, HEIGHT - 1, 0)
    app.sendall(b"\x1b[H\x1b[2@" )
    screen = server.sync(app, terminal)
    has(screen, 0, 2, 12, "405060")
    empty(screen, 0, 0)
    app.sendall(b"\x1b[1P")
    screen = server.sync(app, terminal)
    has(screen, 0, 1, 12, "405060")


def extensions(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[H" + dcs("pen;4095;8;abcdef;1;1;style=dashed;priority=7")
                + dcs("pen-round;255;outside=p3;radius=64") + b"X"
                + b"\x1b[0m\x1b[2;1H"
                + dcs("set;255;8;123456;0;0;1;1;style=dotted")
                + dcs("round;255;1;1;outside=default;radius=32"))
    screen = server.sync(app, terminal)
    cell = screen[0, 0]
    has(screen, 0, 0, 4095, "abcdef")
    assert cell["round"] == 255 and cell["outside"] == "p3" and cell["radius"] == "64", cell
    assert all(value[1:] == ("dashed", "7") for value in cell["segments"].values()), cell
    cell = screen[1, 0]
    assert cell["round"] == 255 and cell["outside"] == "default" and cell["radius"] == "32", cell
    assert all(value[1] == "dotted" for value in cell["segments"].values()), cell
    app.sendall(b"\x1b[3;1H" + dcs(
        "set;3;8;456789;0;0;1;1;future-key=abc;style=double;style=dotted")
        + b"\x1b[4;1H" + dcs(
        "set;3;8;456789;0;0;1;1;style=future;priority=16")
        + b"\x1b[2;1H" + dcs("round;255;1;1;outside=unknown;radius=999"))
    screen = server.sync(app, terminal)
    for row, style in ((2, "dotted"), (3, "solid")):
        has(screen, row, 0, 3, "456789")
        assert all(value[1:] == (style, "0") for value in
                   screen[row, 0]["segments"].values()), screen[row, 0]
    cell = screen[1, 0]
    assert (cell["outside"], cell["radius"]) == ("default", "32"), cell
    app.sendall(b"\x1b[2;1H" + dcs("square;1;1;1;outside=p9;radius=128"))
    screen = server.sync(app, terminal)
    cell = screen[1, 0]
    assert (cell["round"], cell["outside"], cell["radius"]) == (254, "p9", "128"), cell


def wide(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[H" + dcs("pen;255;8;abcdef;0;0")
                + "界".encode() + b"\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 255 & ~12, "abcdef")
    has(screen, 0, 1, 255 & ~192, "abcdef")
    assert not (set(segments(screen, 0, 0)) & {2, 3}), screen[0, 0]
    assert not (set(segments(screen, 0, 1)) & {6, 7}), screen[0, 1]
    app.sendall(b"\x1b[1;2HX")
    screen = server.sync(app, terminal)
    empty(screen, 0, 0)
    empty(screen, 0, 1)


def reflow(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    # The sole bordered cell is a trailing blank: it must count as content.
    app.sendall(b"\x1b[2J\x1b[H" + b"x" * 44
                + dcs("pen;255;8;234567;0;0") + b" " + b"\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 44, 255, "234567")
    server.run("resize-window", "-t", "border", "-x", "40", "-y", str(HEIGHT))
    screen = server.sync(app, terminal)
    width, col, row = map(int, server.run("display-message", "-p",
        "#{pane_width} #{cursor_x} #{cursor_y}").split())
    assert (width, col) == (40, 5), (width, col, row)
    # tmux may push the preceding wrapped line into history while keeping the
    # cursor on the same visible row. The blank belongs just before the cursor.
    has(screen, row, 4, 255, "234567")
    server.run("resize-window", "-t", "border", "-x", str(WIDTH), "-y", str(HEIGHT))
    screen = server.sync(app, terminal)
    width, col, row = map(int, server.run("display-message", "-p",
        "#{pane_width} #{cursor_x} #{cursor_y}").split())
    assert (width, col) == (80, 45), (width, col, row)
    has(screen, row, 44, 255, "234567")


def insufficient(server):
    _, app = server.pane(first=True)
    server.attach(features="pen,rect,half,center")  # Missing mandatory palette.
    assert server.query(app) == [], "incomplete baseline advertised as support"


def screens(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[2J\x1b[H" + dcs("pen;3;8;123456;0;0") + b"A"
                + b"\x1b[?1049h\x1b[0m\x1b[2J\x1b[2;1H"
                + dcs("pen;12;8;abcdef;0;0") + b"B")
    screen = server.sync(app, terminal)
    has(screen, 1, 0, 12, "abcdef")
    empty(screen, 0, 0, "alternate after erase")
    app.sendall(b"\x1b[?1049l\x1b[1;2HC")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 3, "123456")
    has(screen, 0, 1, 3, "123456")
    empty(screen, 1, 0, "main restored")
    app.sendall(b"\x1b[!p\x1b[2;1HD")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 3, "123456")
    empty(screen, 1, 0, "DECSTR clears pen")
    app.sendall(b"\x1bc")
    screen = server.sync(app, terminal)
    assert not any(cell["segments"] for cell in screen.values()), "RIS kept borders"


def live(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[2;3H" + dcs("set;4095;8;abcdef;0;0;2;3"))
    server.query(app)
    screen = terminal.snapshot()  # Deliberately no refresh-client.
    has(screen, 1, 2, 4095, "abcdef")
    has(screen, 2, 4, 4095, "abcdef")
    app.sendall(dcs("clear;4095;2;3"))
    server.query(app)
    screen = terminal.snapshot()
    for row in (1, 2):
        for col in (2, 3, 4):
            empty(screen, row, col, "live rectangle clear")


def synchronized(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    terminal.snapshot()
    with terminal.lock:
        start = len(terminal.frames)
    app.sendall(b"\x1b[?2026h\x1b[H" + dcs("pen;3;8;456789;0;0") + b"X")
    server.query(app)
    terminal.snapshot()
    with terminal.lock:
        emitted = terminal.frames[start:]
    assert not [frame for frame in emitted if frame != "q"], (
        "border emitted before synchronized update ended", emitted)
    app.sendall(b"\x1b[?2026l")
    server.query(app)
    screen = terminal.snapshot()
    has(screen, 0, 0, 3, "456789")


def framing(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[2;3H" + dcs("set;3;8;123456;0;0;1;1"))
    server.query(app)
    has(terminal.snapshot(), 1, 2, 3, "123456")
    # Valid operation prefixes cannot take effect before a complete, valid ST.
    frame = dcs("set;3;8;abcdef;0;0;1;1")
    for offset in range(0, len(frame) - 2, 3):
        app.sendall(frame[offset:min(offset + 3, len(frame) - 2)])
    has(terminal.snapshot(), 1, 2, 3, "123456")
    app.sendall(ST)
    server.query(app)
    has(terminal.snapshot(), 1, 2, 3, "abcdef")
    malformed = dcs("set;3;8;ffffff;0;0;1;1;BAD=x")
    for byte in malformed:
        app.sendall(bytes([byte]))
    app.sendall(dcs("set;3;8;ffffff;0;0;1;1;future=" + "a" * 300)
                + b"\x1bP2+bcell-border;set;3;8;ffffff;0;0;1;1\x1b\\"
                + b"\x1bP1+bnot-cell-border;set;3;8;ffffff;0;0;1;1\x1b\\")
    server.query(app)
    has(terminal.snapshot(), 1, 2, 3, "abcdef")


def palette(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b]4;7;rgb:11/22/33\x1b\\"
                b"\x1b]10;rgb:44/55/66\x1b\\"
                b"\x1b]11;rgb:77/88/99\x1b\\"
                b"\x1b[H" + dcs("set;255;8;p7;0;0;1;1")
                + dcs("round;255;1;1;outside=default;radius=64")
                + b"\x1b[2;1H" + dcs("set;3;8;fg;0;0;1;1"))
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 255, "112233")
    has(screen, 1, 0, 3, "445566")
    assert screen[0, 0]["outside"] == "778899", screen[0, 0]
    app.sendall(b"\x1b]4;7;rgb:aa/bb/cc\x1b\\"
                b"\x1b]10;rgb:dd/ee/ff\x1b\\"
                b"\x1b]11;rgb:ab/cd/ef\x1b\\")
    server.query(app)
    screen = terminal.snapshot()  # Theme updates must repaint, no forced refresh.
    has(screen, 0, 0, 255, "aabbcc")
    has(screen, 1, 0, 3, "ddeeff")
    assert screen[0, 0]["outside"] == "abcdef", screen[0, 0]


def delayed_reply(server):
    _, app = server.pane(first=True)
    server.run("set-option", "-s", "escape-time", "20")
    terminal = server.attach(respond=False)
    assert server.query(app) == []
    reply = dcs("ok;1;" + FULL)
    split = len(PREFIX) + len(b"ok;1;pen,rect")
    os.write(terminal.fd, reply[:split])
    time.sleep(.1)  # Deliberately exceed escape-time while inside the DCS.
    assert not select.select([app.sock], [], [], .05)[0], (
        "partial native terminal reply escaped into pane input")
    os.write(terminal.fd, reply[split:])
    time.sleep(.1)
    assert not select.select([app.sock], [], [], .05)[0], (
        "delayed native terminal reply escaped into pane input")
    assert server.query(app), "delayed reply did not verify capability"
    os.write(terminal.fd, PREFIX + b"ok;1;" + b"a" * 2048)
    time.sleep(.1)
    assert not select.select([app.sock], [], [], .05)[0], (
        "oversized native reply body escaped into pane input")
    os.write(terminal.fd, ST + b"Z")
    assert app.recv(65536) == b"Z", "oversized reply did not drain through ST"
    assert server.query(app), "oversized reply corrupted verified capability"


def pending_wrap(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[3;1H" + b"x" * WIDTH
                + dcs("set;255;8;345678;0;0;1;1"))
    screen = server.sync(app, terminal)
    has(screen, 2, WIDTH - 1, 255, "345678")
    empty(screen, 3, 0, "rectangle must not consume pending wrap")


def copy_mode(server):
    pane, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[H" + dcs("pen;255;8;abcdef;0;0")
                + "界".encode() + b"\x1b[0m")
    server.sync(app, terminal)
    server.run("copy-mode", "-t", pane)
    server.run("refresh-client")
    screen = terminal.snapshot()
    has(screen, 0, 0, 255 & ~12, "abcdef")
    has(screen, 0, 1, 255 & ~192, "abcdef")
    assert not set(segments(screen, 0, 0)) & {2, 3}, screen[0, 0]
    assert not set(segments(screen, 0, 1)) & {6, 7}, screen[0, 1]
    server.run("send-keys", "-t", pane, "-X", "cancel")


def one_column(server):
    server.pane(first=True)
    pane, app = server.pane()
    server.run("resize-pane", "-t", pane, "-x", "1")
    terminal = server.attach()
    left, top, width = map(int, server.run("display-message", "-p", "-t", pane,
        "#{pane_left} #{pane_top} #{pane_width}").split())
    assert width == 1, width
    terminal.snapshot()
    with terminal.lock:
        start = len(terminal.frames)
    app.sendall(b"\x1b[?2026h\x1b[H" + dcs("pen;255;8;abcdef;0;0") + b"X")
    server.query(app)
    terminal.snapshot()
    with terminal.lock:
        emitted = terminal.frames[start:]
    assert not [frame for frame in emitted if frame != "q"], (
        "one-column synchronized pane drew early", emitted)
    app.sendall(b"\x1b[?2026l")
    server.query(app)
    has(terminal.snapshot(), top, left, 255, "abcdef")


def horizontal_tab(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[1;4H" + dcs("set;3;8;123456;0;0;1;1")
                + b"\x1b[H" + dcs("pen;12;8;abcdef;0;0") + b"\tX\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 3, 3, "123456")
    for col in (0, 1, 2, 4, 5, 6, 7):
        empty(screen, 0, col, "HT must only move cursor")
    has(screen, 0, 8, 12, "abcdef")


def alignment(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(dcs("pen;3;8;123456;0;0") + b"\x1b#8\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 3, "123456")
    has(screen, HEIGHT - 1, WIDTH - 1, 3, "123456")


def save_1048(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[H" + dcs("pen;3;8;123456;0;0")
                + b"\x1b[?1048h" + dcs("pen-clear;4095")
                + dcs("pen;12;8;abcdef;0;0") + b"A\x1b[?1048l"
                + b"\x1b[1;2HB\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 12, "abcdef")
    has(screen, 0, 1, 3, "123456")
    assert not set(segments(screen, 0, 1)) & {2, 3}, screen[0, 1]


def capability_tokens(server):
    _, app = server.pane(first=True)
    server.attach(features=FULL + ",feature2")
    replies = server.query(app)
    assert len(replies) == 1 and set(replies[0].decode().split(",")) == set(
        FULL.split(",")), ("unknown printable capability rejected", replies)


def vs16(server):
    _, app = server.pane(first=True)
    terminal = server.attach()
    app.sendall(b"\x1b[H" + dcs("pen;255;8;abcdef;0;0")
                + "\u2764\ufe0f".encode() + b"\x1b[0m")
    screen = server.sync(app, terminal)
    has(screen, 0, 0, 255 & ~12, "abcdef")
    has(screen, 0, 1, 255 & ~192, "abcdef")
    assert not set(segments(screen, 0, 0)) & {2, 3}, screen[0, 0]
    assert not set(segments(screen, 0, 1)) & {6, 7}, screen[0, 1]


TESTS = {test.__name__: test for test in
         (negotiation, insufficient, state, offsets, movement, extensions, wide,
          reflow, screens, live, synchronized, framing, palette, delayed_reply,
          pending_wrap, copy_mode, one_column, horizontal_tab, alignment,
          save_1048, capability_tokens, vs16)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    parser.add_argument("--test", action="append", choices=TESTS)
    parser.add_argument("--option", action="append", default=[],
                        help="extra tmux config line (repeatable)")
    parser.add_argument("--capture-dir",
                        help="retain TEST/apps.bin, outer.bin and app-input.bin")
    parser.add_argument("--source-sha", help="source revision recorded in metadata")
    parser.add_argument("--binary-sha256", help="verify expected binary SHA256")
    args = parser.parse_args()
    binary = os.path.abspath(args.binary)
    with open(binary, "rb") as file:
        binary_hash = hashlib.file_digest(file, "sha256").hexdigest()
    if args.binary_sha256 and binary_hash != args.binary_sha256:
        parser.error("binary SHA256 does not match --binary-sha256")
    provenance = {"binary_sha256": binary_hash, "source_sha": args.source_sha,
                  "spec_sha256": "76cd38dd5fd3565ec5ea9989f3fba45bd66b8cedb9da7b90a01138fd684a45be"}
    with open(os.path.abspath(__file__), "rb") as file:
        provenance["fixture_sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
    failures = []
    for name in args.test or TESTS:
        with tempfile.TemporaryDirectory(prefix="tmux-cell-border-") as root:
            capture = os.path.join(os.path.abspath(args.capture_dir), name) if (
                args.capture_dir) else None
            server = Server(binary, root, args.option, capture, provenance)
            try:
                TESTS[name](server)
                print("PASS", name, flush=True)
            except Exception as error:
                failures.append(name)
                print("FAIL", name, repr(error), flush=True)
            finally:
                server.close()
    print("%d passed, %d failed; wire metadata only, no rendered proof" %
          (len(args.test or TESTS) - len(failures), len(failures)))
    return bool(failures)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--helper":
        helper(sys.argv[2])
    else:
        sys.exit(main())
