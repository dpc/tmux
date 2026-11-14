# Native cell borders

tmux retains cell borders in its grid instead of forwarding application drawing
commands to the outer terminal. Borders follow pane offsets, history,
copy-mode display and reflow. Applications still need to repaint their layout
after a resize; tmux moves cells, not application widgets.

## Draft contract

This implementation targets Joshka's experimental cell-border v1 draft, not a
registered terminal standard:

* Repository: `joshka/ghostty`
* Commit: `62f25299908b4fc432dca76f330bf43da7185a02`
* File: `specs/cell-border-protocol.md`
* SHA256: `76cd38dd5fd3565ec5ea9989f3fba45bd66b8cedb9da7b90a01138fd684a45be`

Native framing is `ESC P 1 + b cell-border;OP;... ESC \`, without the illustrative
spaces. tmux validates the complete payload before changing state. The payload
limit is 256 bytes. Unknown operations, invalid positional values, malformed
extension syntax and oversized payloads have no effect. Unknown syntactically
valid extension keys and values are ignored; the last recognized value wins.

All draft commands and optional fields are retained: pen and rectangle segment
edits, joint edits, palette/default colours, styles, priority, outside fill and
radius. SGR reset clears the pen. Cursor save/restore, including mode 1048,
preserves it. Erasing and inserting blank cells applies the current pen; moving
existing cells preserves their attributes. Wide-glyph printing omits internal
edges, but rectangles can independently edit either physical cell.

## Capability negotiation

Each attached terminal receives a native query before primary device attributes.
tmux replies to a pane's query only when every attached, non-control client whose
session contains that pane's window has confirmed the mandatory baseline:
`pen,rect,half,center,palette`. The reply advertises the intersection of their
features, not tmux's internal storage capability or a guess from `TERM`.
Detached and control-only sessions receive no positive reply.

Identified client replies stay out of keyboard input, even when transport splits
exceed `escape-time`. Oversized replies drain through their terminator without
retaining the whole frame. Bracketed-paste contents remain literal input.

An unsupported client can still display the text of a previously bordered
application. Attaching it does not rewrite application content into box-drawing
characters. Applications choose fallback drawing based on their query results.

## Storage and redraw

Border values live in normal and extended grid cells. There is no separately
owned overlay, unbounded intern table or borrowed pointer to an input frame.
History, line moves, duplication and reflow use the existing grid lifetime.
Bordered blanks count as content. Copy mode preserves physical attributes,
including independent edits to a wide character's second cell; copied text
does not contain border protocol commands.

Once a pane uses borders, tmux conservatively requests full pane redraws for
ordinary updates. Synchronized output still records dirty rows and waits for
release. The normal renderer writes text with an empty outer pen, then restores
physical cell borders using native rectangle commands at clipped terminal
coordinates. Pane palette and default-colour overrides resolve during redraw,
so OSC colour changes update existing borders.

This favors correctness over output efficiency. Extended cells are larger,
including extended cells without borders; a bordered redraw can send one
command per segment per cell. No performance benchmark is claimed.

## Scope and limitations

The full proposed border command set is implemented, but this does not add a
general protected-character subsystem to tmux. Unsupported DECSCA, DECSED and
DECSEL remain unsupported for both text and borders. The draft's capability
names describe border commands and geometry, not support for those baseline VT
operations.

Native border output never uses passthrough. Explicit `allow-passthrough`
wrappers retain their existing arbitrary-output behavior. Wrapped border
commands bypass pane coordinates and retained state and are not supported usage
of this implementation.

No support is inferred from the terminal name. Unknown terminal implementations
and future draft revisions require fresh capability and compatibility testing.

## Verification

Build with the project's normal autotools workflow. One Nix development-shell
invocation is:

```sh
nix-shell -p autoconf automake pkg-config libevent ncurses bison \
  --run './autogen.sh && ./configure && make -j8'
python3 regress/cell-border.py /absolute/path/to/tmux
```

The 22-scenario regression uses private servers, PTYs and mock terminal clients.
It checks negotiation, malformed and fragmented input, retained edits, pane
offsets, scrolling, reflow, wide glyphs and combining expansion, copy mode,
palette changes, synchronized output, cursor motion, alignment and saved state.
It also checks existing output without forcing a refresh.

For reproducible wire captures:

```sh
python3 regress/cell-border.py /absolute/path/to/tmux \
  --capture-dir /tmp/cell-border-captures --source-sha COMMIT
```

Each scenario records application output, outer-terminal output, application
input and JSON metadata. Metadata includes source identity, binary, fixture and
spec hashes, plus geometry changes. Capture stops before server teardown.

These tests check generated wire state, not pixels. They do not by themselves
establish rendered WezTerm, SSH or nested-terminal correctness. Existing fork
clipboard and cursor-style regressions remain separate required checks for the
combined branch.
