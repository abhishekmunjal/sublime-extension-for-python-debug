"""
debug_launcher.py

Runs a target Python script under a hand-rolled line-stepper built directly
on sys.settrace — the same primitive pdb/bdb are built on, used here with
none of pdb's terminal/cmdloop assumptions.

The debug protocol runs over its OWN local TCP socket (127.0.0.1), separate
from the target script's real stdin/stdout/stderr. This matters: if the
protocol shared stdout with the target script (an earlier version of this
file did exactly that), anything the script printed would land in the same
stream as our JSON messages and corrupt them. Keeping them separate also
means the target script's own input()/print() work completely normally.

Protocol (over the socket, one JSON object per line):
  OUT (this process -> Sublime plugin):
    {"event": "line", "file": ..., "line": ..., "function": ..., "locals": {...}}
    {"event": "exception", "file": ..., "line": ..., "type": ..., "value": ...}
    {"event": "error", "message": "<traceback text>"}
    {"event": "done"}

  IN (Sublime plugin -> this process), one command per line:
    "step"      -> step into: stop at the very next line executed, anywhere
    "next"      -> step over: stop at the next line in the SAME frame
                   (calls made from this line run to completion without stopping)
    "continue"  -> run until the program ends (no breakpoints yet)
    "quit"      -> stop debugging, let the rest of the script run untraced

How the tracing actually works (the part worth reading closely):

sys.settrace(global_trace) installs `global_trace` as the function called
every time a NEW frame is entered (a 'call' event). Whatever `global_trace`
returns becomes that frame's LOCAL trace function, which is then called for
every 'line', 'return', and 'exception' event inside that specific frame.
Returning None from the global trace means "don't bother tracing this frame
or anything it calls" -- that's how we avoid instrumenting our own internals.
"""

import sys
import os
import json
import socket
import traceback


LAUNCHER_FILE = os.path.abspath(__file__)

# Locals that are always present and never useful to show.
NOISY_LOCAL_NAMES = {"__builtins__"}


class DebugQuit(Exception):
    """Raised internally to unwind out of the traced program on 'quit'."""
    pass


class Tracer:
    def __init__(self, reader, writer):
        self.mode = "step"        # 'step' | 'next' | 'continue'
        self.target_frame = None  # for 'next': the frame we must return to
        self.paused = False       # reentrancy guard, see trace_calls()
        self.reader = reader      # socket file object, protocol IN
        self.writer = writer      # socket file object, protocol OUT

    # --- sys.settrace entry point: fires on every new frame ('call' event) ---
    def trace_calls(self, frame, event, arg):
        if self.paused:
            # We're inside our own emit/read-command code right now (e.g.
            # json.dumps, sys.stdin.readline create their own frames) --
            # do NOT instrument those, or we'd recursively trap ourselves.
            return None
        if frame.f_code.co_filename == LAUNCHER_FILE:
            # Never trace this file's own functions.
            return None
        return self.trace_lines

    # --- local trace function: fires for line/return/exception in one frame ---
    def trace_lines(self, frame, event, arg):
        if self.paused:
            return self.trace_lines

        if event == "line":
            if self.mode == "step":
                self._stop(frame)
            elif self.mode == "next":
                if self.target_frame is None or frame is self.target_frame:
                    self._stop(frame)
            # mode == "continue": fall through, let it run

        elif event == "return":
            if self.mode == "next" and frame is self.target_frame:
                # The frame we were stepping over just returned -- move the
                # target up to the caller so the NEXT line event (back in
                # the caller) is the one that triggers a stop.
                self.target_frame = frame.f_back

        elif event == "exception":
            exc_type, exc_value, _tb = arg
            self._emit({
                "event": "exception",
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "type": exc_type.__name__,
                "value": str(exc_value),
            })

        return self.trace_lines

    # --- stop execution here, report state, block for the next command ---
    def _stop(self, frame):
        self.paused = True
        try:
            self._emit_line(frame)
            cmd = self._read_command()
        finally:
            self.paused = False

        if cmd == "step":
            self.mode = "step"
            self.target_frame = None
        elif cmd == "next":
            self.mode = "next"
            self.target_frame = frame
        elif cmd == "continue":
            self.mode = "continue"
            self.target_frame = None
        elif cmd == "quit" or cmd is None:
            raise DebugQuit()
        else:
            # unrecognized command: default to step-over, stay safe
            self.mode = "next"
            self.target_frame = frame

    def _emit_line(self, frame):
        locals_repr = {}
        for name, value in frame.f_locals.items():
            if name in NOISY_LOCAL_NAMES:
                continue
            try:
                locals_repr[name] = repr(value)
            except Exception:
                locals_repr[name] = "<unrepresentable>"
        self._emit({
            "event": "line",
            "file": frame.f_code.co_filename,
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
            "locals": locals_repr,
        })

    def _read_command(self):
        raw = self.reader.readline()
        if not raw:
            return None
        return raw.strip()

    def _emit(self, msg):
        self.writer.write(json.dumps(msg) + "\n")
        self.writer.flush()


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: debug_launcher.py <target_script.py> <protocol_port>\n")
        sys.exit(1)

    target = os.path.abspath(sys.argv[1])
    port = int(sys.argv[2])

    # Connect back to the plugin's waiting server socket. This is the ONLY
    # channel used for the debug protocol -- the target script's own
    # sys.stdin/stdout/stderr are left completely untouched.
    sock = socket.create_connection(("127.0.0.1", port))
    reader = sock.makefile("r", encoding="utf-8", newline="\n")
    writer = sock.makefile("w", encoding="utf-8", newline="\n")

    tracer = Tracer(reader, writer)

    with open(target, "r") as f:
        source = f.read()
    code = compile(source, target, "exec")
    script_globals = {
        "__name__": "__main__",
        "__file__": target,
    }

    sys.settrace(tracer.trace_calls)
    try:
        exec(code, script_globals)
    except DebugQuit:
        pass
    except SystemExit:
        pass
    except Exception:
        tracer._emit({"event": "error", "message": traceback.format_exc()})
    finally:
        sys.settrace(None)
        tracer._emit({"event": "done"})
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
