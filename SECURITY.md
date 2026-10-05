# Native OSC5522 mediation

**Native mediation is off by default.** It requires an explicit declaration
about the immediate terminal peer, as well as a successful capability probe.
The declaration is a local trust decision, not a property inferred from a
terminal name, version, TERM value, or mode report.

## Configuration and per-attachment overrides

For ordinary local or SSH attachments, put this in `tmux.conf`:

```tmux
set -s native-clipboard on
```

This server option is off by default. Enabling it declares that the immediate
terminal peers provide the cancellation contract below; a successful outer
capability probe is still required for each attachment. No launch wrapper,
terminal-name matching, or provenance helper is needed. The option applies live
to existing attachments and to ordinary `new-session` and `attach-session`
clients. It is independent of the legacy OSC52 `set-clipboard` option.

Optional per-attachment overrides take precedence over the server option:

```sh
tmux attach-session -f clipboard-fence -t my-session
tmux new-session -f clipboard-fence -s my-session

# Inspect and explicitly opt in just one existing attachment.
tmux list-clients -F '#{client_name}: #{client_flags} #{client_clipboard_state}'
tmux refresh-client -t /dev/pts/123 -f clipboard-fence

# Explicitly disable this attachment even when native-clipboard is on.
tmux refresh-client -t /dev/pts/123 -f '!clipboard-fence'
```

Replace the example client name with the intended attachment. An explicit
`clipboard-fence` remains enabled when the server option is turned off;
`!clipboard-fence` remains disabled when it is turned on. A new attachment
without either flag inherits the server option. `client_flags` shows explicit
overrides, not the inherited option value.

Enable before starting a MIME-aware program, or have that program re-probe after
changing the option or flags. If the attachment's outer probe failed, reattach
to probe again. Each nested tmux server must enable
the option (or explicitly enable its attachment), and every hop must run the
patched mediator with a verified outer attachment. There is no automatic trust
propagation.

The declared peer contract is stronger than generic OSC5522 capability:

* one ordered response writer;
* mode-off revokes the old clipboard generation, including asynchronous work;
* an already-started packet finishes before subsequent fence responses;
* no old asynchronous producer can emit an old packet after fence completion.

The paired WezTerm implementation and a chain of patched tmux mediators must
establish these properties in code and tests. A generic terminal capability
report does not establish them. Without opt-in, pane probes report unsupported
and ordinary legacy text paste remains available. Unknown peers must stay off.
Disabling mediation revokes routing even with automatic paste mode disabled.
Cleanup obligations from the previously verified attachment remain; toggling the
option or flags cannot erase framing state or release a quarantined attachment.

## Trust and authority

tmux mediates the Kitty clipboard extension and automatic-paste mode 5522.
The outer terminal owns native clipboard access and its permission policy.
tmux does not read local paths, interpret URI lists, decode/assemble clipboard
objects, select a preferred MIME type, or apply Tau's artifact-ingestion limit.
Reads, writes, write chunks, aliases, primary selection, and arbitrary MIME
payloads are routed rather than stored as tmux paste buffers.

Native OSC packets require the canonical `ESC ] 5522 ;` introducer.
Leading-zero, integer-overflow, and control-stripped numeric aliases are not
accepted through the generic OSC parser.

Only explicit opt-in plus a successful outer probe permit advertising support.
Each transaction receives a fresh random outer ID; replies restore its original
ID and belong to its original pane. One pending transaction per attachment and
per pane prevents continuation packets from changing attachments. Contending
requests receive EBUSY. Unknown/stale reply IDs are consumed, never keyboard
input. Active-pane selection controls unsolicited offers and mode propagation,
not ordinary background-pane read permission.

Captured paste grants bind to pane, attachment and location. Valid metadata is
preserved. Known foreign or revoked grants are denied locally; unknown tokens and
ordinary ungranted requests retain the terminal's permission decision. A missing
name is not grant authority. Content DATA proves admission; dot listings and
unavailable-only results do not spend an unused grant in the mux.

Each attachment retains up to 64 revoked tokens and 64 KiB of token strings.
Tracking pressure fails closed for unknown password-bearing requests rather
than forgetting potentially live authority. Ordinary requests without passwords
remain available. Switching owner, focus, pane mode, or session revokes routing
and sends mode-off even when the next pane also enables mode 5522.

## Framing, cancellation and limits

Before the complete 5522 introducer is recognized, normal tmux `escape-time`
disambiguation applies. After recognition, arbitrary transport splits retain a
bounded parser state. Invalid or excessive frames are discarded through their
terminator, not returned as keys. Bracketed-paste contents bypass protocol
recognition and remain literal text. Native pane OSC input must not inherit
generic OSC control-byte stripping: malformed writes abort rather than commit
a truncated valid prefix.

Incoming OSC frames are bounded to 1 MiB; mode reports to 64 body bytes. The
existing configurable pane input buffer bounds outgoing individual OSC frames.
These are per-frame limits, not aggregate clipboard limits. No entire clipboard
object is accumulated. A 256 KiB queue watermark pauses the producer and resumes
from local write-drain callbacks. Protocol output cannot enter tmux's lossy
display-output discard path.

The capability probe has a one-second deadline. Grants and inactive
transactions have 30-second deadlines. Read errors are only opening errors;
an interrupted read after OK has no invented midstream error or synthetic DONE.
Its consumer must discard incomplete data. Write failure drains subsequent
write-related packets until a new opening write.

Cancellation tracks queued packet spans. An already-started packet finishes
before mode-off; unstarted protocol output can be removed. For an attachment
that used the verified service, lock, suspend, detach and exec register a fresh
random ID, queue mode-off followed by a standard dot-MIME inventory read, and
consume that response locally. Success requires a full matching
OK / DATA with dot MIME / DONE sequence, clear incoming framing, and drained
output. Inventory and grant information are never published to a pane.
Errors, stale IDs, a nonce-less mode report, a lone terminator or a swallowed
opening OK do not prove the boundary. There is no nested escape-code recovery
heuristic.

### Bounded failure is not a successful lock

The entire handoff has a **two-second deadline**, not a new deadline per packet.
Failure returns a command error and quarantines only the affected attachment.
It sends no lock/suspend/exec/detach message, starts no external reader, and
keeps sole ownership of bounded input discard. Pane input, display output,
passthrough and native requests through that attachment are suppressed; panes
and other clients continue running.

This is **not an authenticated or successful lock**. Stalled output can leave
old screen contents visible and prevent an on-screen notice. Failure is reported
to the command caller and through the control-mode notification
`%clipboard-handoff-failed CLIENT quarantined not-locked`.
Inspect `client_clipboard_state` from another trusted client:

* `inactive`: no active clipboard attachment state;
* `normal`: not in a handoff (check `native-clipboard` and `client_flags` for opt-in);
* `draining`: a handoff is pending, for at most two seconds;
* `quarantined`: the operation failed and the boundary is still unproven;
* `quarantined-ready`: complete late proof arrived, but isolation remains.

Only an **explicit retry of the intended handoff** after proven recovery can
release quarantine. Late replies, focus events, refresh, flag removal/re-addition
or ordinary wakeup do not do so automatically. Otherwise close the affected
terminal/SSH connection and reconnect using a fresh terminal input stream.
Force-killing only the tmux client can expose the outer shell and residual
bytes; it is not a secure unlock or input cleanup mechanism.

An unclean transport loss cannot guarantee delivery of an unfinished frame or
mode-off. Startup ST followed by mode-off resets the *outgoing terminal request
parser* and revokes outer authority before probing; it does not prove that
arbitrary old incoming bytes have disappeared. No nonce-less DECRPM response is
treated as an authenticated freshness fence.

These guarantees concern tmux's **client TTY handoff**. A pane application
disabling mode 5522 cannot retract bytes already queued/written into its pane's
PTY. Its own external-editor/raw-reader handoff needs its own input ownership
protocol; do not infer end-to-end foreground-reader safety from tmux client
handoff tests.

## Diagnostics

Raw pane-input, OSC-dispatch, DCS-passthrough and TTY-add diagnostics are reduced
to lengths where split clipboard metadata could appear. Native protocol output
bypasses debug and raw TTY logs. Arbitrary DCS passthrough also bypasses raw TTY
logging because selective redaction across split wrappers cannot be trusted.
Other structured diagnostics remain available. This does not conceal content
that an application deliberately renders or an administrator explicitly captures
with pane piping/control-mode output.

## Verification

`python3 regress/clipboard.py ./tmux` uses only private temporary servers, PTYs
and mock terminal/application endpoints. It covers native offers, PNG and text,
primary selection, grant isolation, ordinary permissions, chunk fidelity,
write aliases/error draining, malformed input, nested ID mapping, multiple
clients, focus changes, both-direction backpressure, a write larger than 64 MiB,
legacy paste, debug privacy, full nested fences, and successful/failed handoffs
with command errors, control notices and explicit quarantine recovery. A
pane-facing FIFO test also verifies that an application which keeps reading
through mode-off and a fresh metadata reply receives the full old packet suffix
before that reply, with late old responses suppressed across attachments.
Protected-output pressure tests verify display recovery after protocol drain.

These tests do not establish the native desktop backend's correctness, provide a
real clipboard permission prompt, or prove end-to-end WezTerm/SSH/Tau operation.
The wire contract follows the August 4, 2026 automatic-paste correction:
MIME lists are dot-MIME DATA payloads; requested types are in the read payload.
Protocol sources are Kitty's clipboard extension and rockorager's automatic
paste mode specification. Initial shared synthetic examples were the
`tau-osc5522-transcripts-v2` handoff, not captured terminal executions.
