# sublime_pdb_debugger

A minimal line-by-line Python debugger for Sublime Text, built directly on
`sys.settrace` (the same primitive `pdb`/`bdb` are built on) rather than
wrapping `pdb` itself.

## How it works

- `debug_launcher.py` runs as a **separate subprocess**, executing your
  target script under a hand-written tracer (`Tracer` class).
- `sublime_pdb_plugin.py` is the Sublime-side plugin: it opens a local TCP
  socket, starts the subprocess (passing it the port to connect back to),
  highlights the current line, and shows local variables in an output panel.
- The debug protocol (JSON messages, one per line) runs entirely over that
  socket — **not** over the subprocess's stdin/stdout. The target script's
  own `print()`/`input()` are left completely untouched, going through the
  normal stdout/stdin pipe, and are shown in the panel as plain `[output]`
  lines. (An earlier version of this shared stdout for both, and a script
  printing anything corrupted the JSON stream — worth knowing if you ever
  see that failure mode elsewhere.)
- They're deliberately decoupled: the launcher doesn't know it's talking to
  Sublime, and the plugin doesn't know anything about tracing internals. You
  could swap the front end (e.g. drive it from VS Code, or the terminal) by
  reusing `debug_launcher.py` unchanged.

Read the docstring at the top of `debug_launcher.py` for a full explanation
of the `sys.settrace` mechanism (call/line/return/exception events, the
global-trace-vs-local-trace split, and why the `paused` flag exists).

## Install

1. Open Sublime Text → `Preferences: Browse Packages...`
2. Create a folder there named `sublime_pdb_debugger`
3. Copy these four files into it:
   - `debug_launcher.py`
   - `sublime_pdb_plugin.py`
   - `Default.sublime-keymap`
   - `sublime_pdb_debugger.sublime-settings`
4. Restart Sublime Text (or `Preferences: Refresh Packages` from the palette).

If your target scripts need a specific Python (e.g. a venv), edit
`python_bin` in `sublime_pdb_debugger.sublime-settings` to a full path, e.g.
`/usr/bin/python3.11` or `/path/to/venv/bin/python`.

## Usage

Open the `.py` file you want to debug, save it, then:

| Key        | Action                                                        | Mac Keys
|------------|----------------------------------------------------------------|
| `F5`       | Start a debug session for the active file                     | `Cmd+Shift+D`
| `F10`      | Step over — run the current line, don't descend into calls it makes | `Cmd+Shift+O`
| `F11`      | Step into — stop at the very next line executed, anywhere      | `Cmd+Shift+I`
| `F8`       | Continue — run to completion (no breakpoints in this version)  | `Cmd+Shift+C`
| `Shift+F5` | Stop the session                                                | `Cmd+Shift+K`


The current line highlights in the editor, and an output panel at the
bottom shows the current function name and every local variable's `repr()`
at that point.

## Known limitations (this is a v1)

- No breakpoints yet — `continue` always runs to the end. Adding them is a
  natural next step: keep a set of `(file, line)` pairs, and in `trace_lines`
  check `mode == "continue"` against that set before deciding whether to
  call `_stop`.
- One session per Sublime window at a time.
- Errors from the target script (e.g. a real exception that terminates it)
  are shown as an `error` event in the panel with the full traceback text.

## Suggested next experiments (good for the "ground-up" goal)

- Add a `print(frame.f_code.co_names)` inside `_emit_line` and watch how it
  differs from `f_locals` — this is a good bridge into how CPython's code
  objects separate compile-time name tables from runtime value bindings.
- Try tracing a generator function and watch how `call`/`line`/`return`
  events fire across `yield` — you'll see the "pause and resume" behavior
  from Layer 5 directly in the trace stream instead of just reading about it.
