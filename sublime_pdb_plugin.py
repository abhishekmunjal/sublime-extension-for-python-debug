"""
sublime_pdb_plugin.py

Sublime Text 4 plugin: launches debug_launcher.py as a subprocess for the
current file, talks to it over a local socket (the debug protocol), and
reflects the state in the editor (current-line highlight + a locals panel).

The target script's own stdout/stderr are NOT part of the debug protocol --
they're read from the subprocess's normal stdout pipe and shown in the panel
as plain "[output]" lines, completely separate from the JSON messages that
arrive over the socket. This is what lets the target script print() freely
without corrupting the debugger's own communication.

Commands (bind these in a keymap, see Default.sublime-keymap):
    pdb_debug_start        - launch a debug session for the active file
    pdb_debug_step         - step into
    pdb_debug_step_over    - step over (called "next" in the protocol)
    pdb_debug_continue     - run to completion
    pdb_debug_stop         - end the session
"""

import sublime
import sublime_plugin
import subprocess
import threading
import socket
import json
import os

LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_launcher.py")
REGION_KEY = "pdb_current_line"
PANEL_NAME = "pdb_locals"

# One session per window, keyed by window id.
sessions = {}


class DebugSession:
    def __init__(self, window, python_bin, target_path):
        self.window = window
        self.target_path = target_path
        self.view_for_target = None
        self.writer = None
        self.panel = window.create_output_panel(PANEL_NAME)

        # Listen on an ephemeral local port; the subprocess connects back to
        # it to carry the debug protocol, entirely separate from its stdout.
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.bind(("127.0.0.1", 0))
        self.server_sock.listen(1)
        port = self.server_sock.getsockname()[1]

        self.process = subprocess.Popen(
            [python_bin, "-u", LAUNCHER_PATH, target_path, str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.path.dirname(target_path),
        )

        threading.Thread(target=self._accept_and_read_protocol, daemon=True).start()
        threading.Thread(target=self._read_program_output, daemon=True).start()

    # --- sending commands to the debuggee, over the protocol socket ---
    def send(self, command):
        if not self.writer:
            return
        try:
            self.writer.write(command + "\n")
            self.writer.flush()
        except OSError:
            pass

    # --- background thread: accept the subprocess's connection, read JSON lines ---
    def _accept_and_read_protocol(self):
        try:
            conn, _addr = self.server_sock.accept()
        except OSError:
            return
        self.writer = conn.makefile("w", encoding="utf-8", newline="\n")
        reader = conn.makefile("r", encoding="utf-8", newline="\n")
        for line in reader:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            sublime.set_timeout(lambda m=msg: self._handle_message(m), 0)

    # --- background thread: the target script's OWN stdout/stderr, plain text ---
    def _read_program_output(self):
        stdout = self.process.stdout
        for raw in iter(stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            if text:
                sublime.set_timeout(lambda t=text: self._append_panel("[output] " + t), 0)

    # --- main-thread handlers (Sublime API calls must happen on the main thread) ---
    def _handle_message(self, msg):
        event = msg.get("event")
        if event == "line":
            self._on_line(msg)
        elif event == "exception":
            self._append_panel("!! {}: {}  (line {})".format(
                msg.get("type"), msg.get("value"), msg.get("line")))
        elif event == "error":
            self._append_panel("ERROR:\n" + msg.get("message", ""))
        elif event == "done":
            self._append_panel("--- session finished ---")
            self._clear_highlight()

    def _on_line(self, msg):
        view = self._view_for_file(msg.get("file"))
        if view is None:
            return
        self.view_for_target = view

        line_no = msg.get("line", 1)
        pt = view.text_point(line_no - 1, 0)
        region = sublime.Region(pt, view.line(pt).b)
        view.add_regions(
            REGION_KEY, [region], "region.yellowish", "bookmark",
            sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE,
        )
        view.show_at_center(pt)
        self.window.focus_view(view)

        locals_dict = msg.get("locals", {})
        text = "{}() line {}\n\nLocals:\n".format(msg.get("function", "?"), line_no)
        for name, value in locals_dict.items():
            text += "  {} = {}\n".format(name, value)
        self._set_panel(text)

    def _view_for_file(self, file_path):
        if not file_path:
            return None
        view = self.window.find_open_file(file_path)
        if view is None:
            view = self.window.open_file(file_path)
        return view

    def _set_panel(self, text):
        self.window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        self.panel.set_read_only(False)
        self.panel.run_command("select_all")
        self.panel.run_command("right_delete")
        self.panel.run_command("append", {"characters": text})
        self.panel.set_read_only(True)

    def _append_panel(self, text):
        self.window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        self.panel.set_read_only(False)
        self.panel.run_command("append", {"characters": "\n" + text + "\n"})
        self.panel.set_read_only(True)

    def _clear_highlight(self):
        if self.view_for_target:
            self.view_for_target.erase_regions(REGION_KEY)

    def stop(self):
        self.send("quit")
        try:
            self.process.terminate()
        except Exception:
            pass
        try:
            self.server_sock.close()
        except OSError:
            pass
        self._clear_highlight()


class PdbDebugStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view or not view.file_name():
            sublime.error_message("Save the file before starting a debug session.")
            return
        if view.is_dirty():
            view.run_command("save")

        existing = sessions.get(self.window.id())
        if existing:
            existing.stop()

        settings = sublime.load_settings("sublime_pdb_debugger.sublime-settings")
        python_bin = settings.get("python_bin", "python3")

        session = DebugSession(self.window, python_bin, view.file_name())
        sessions[self.window.id()] = session
        sublime.status_message("Debug session started")


class PdbDebugStepCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = sessions.get(self.window.id())
        if session:
            session.send("step")


class PdbDebugStepOverCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = sessions.get(self.window.id())
        if session:
            session.send("next")


class PdbDebugContinueCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = sessions.get(self.window.id())
        if session:
            session.send("continue")


class PdbDebugStopCommand(sublime_plugin.WindowCommand):
    def run(self):
        session = sessions.pop(self.window.id(), None)
        if session:
            session.stop()
