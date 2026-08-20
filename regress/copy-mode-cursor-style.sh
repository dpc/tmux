#!/bin/sh

PATH=/bin:/usr/bin
TERM=screen

[ -z "$TEST_TMUX" ] && TEST_TMUX=$(readlink -f ../tmux)

python3 - "$TEST_TMUX" <<'PY'
import os
import select
import signal
import subprocess
import sys
import time

tmux = sys.argv[1]
label = "testA%d" % os.getpid()
server = [tmux, "-L" + label, "-f/dev/null"]

def run(*args, check=True):
    return subprocess.run(server + list(args), check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def attach():
    pid, fd = os.forkpty()
    if pid == 0:
        os.environ["TERM"] = "screen"
        os.execl(tmux, tmux, "-L" + label, "-f/dev/null", "attach-session",
            "-t", "cursor")
    os.set_blocking(fd, False)
    return pid, fd

def read_until(fd, needle, timeout=5):
    end = time.time() + timeout
    data = b""
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if fd not in ready:
            continue
        try:
            data += os.read(fd, 4096)
        except BlockingIOError:
            continue
        if needle in data:
            return
    raise AssertionError("did not see cursor style %r in %r" % (needle, data))

def cleanup(pid=None):
    if pid is not None:
        try:
            os.kill(pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
    run("kill-server", check=False)

run("kill-server", check=False)
run("new-session", "-d", "-x", "80", "-y", "24", "-s", "cursor", "cat")
run("set-option", "-as", "terminal-features", "*:cstyle")
run("set-option", "-gw", "cursor-style", "blinking-bar")
run("set-option", "-w", "copy-mode-cursor-style", "block")

pid, fd = attach()
try:
    read_until(fd, b"\033[5 q")
    run("copy-mode", "-t", "cursor:0.0")
    read_until(fd, b"\033[2 q")
    run("send-keys", "-t", "cursor:0.0", "-X", "cancel")
    read_until(fd, b"\033[5 q")
finally:
    cleanup(pid)
PY
