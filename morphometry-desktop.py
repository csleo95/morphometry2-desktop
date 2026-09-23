#!/usr/bin/env python3
# ---------------------------------------------------------------------------
#  ENIGMA multi-phenotype morphometry - desktop launcher
#
#  Runs on Linux, macOS and Windows via Docker. Needs runtime.py beside it.
#  Standard library only: no pip install.
# ---------------------------------------------------------------------------

import os
import queue
import subprocess
from collections import deque
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import runtime as rt

APP_TITLE = "ENIGMA multi-phenotype morphometry"

# The protocol. Every site runs these unchanged - that is what makes the
# results poolable.
PIPELINE_FLAGS = [
    "--cortical-measures", "recon-all", "after2.curvs", "after2.shfd",
                           "after2.lgi", "after2.gwc", "after2.myelin", "mind",
    "--subcortical-measures", "subcorticalshape", "scmind", "subseg", "segpve",
    "--qc-measures", "euler", "mriqc", "fsqc",
    "--after2-surface", "retess",
    "--segpve-surfaces", "native", "retess",
    "--mind-variants", "nonfiltered", "filtered",
    "--resample",
    "--resample-targets", "fsaverage6",
    "--resample-measures", "recon-all",
    "--parcellate",
    "--parcellations", "HCPMMP1",
    "--parcellate-measures", "recon-all",
]

HINT = "#888"


class Launcher(ttk.Frame):

    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=14)
        self.root = root
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self._marked: dict[str, int] = {}         # log blocks that get rewritten
        self.lines: queue.Queue = queue.Queue()
        self.report: rt.DatasetReport | None = None

        self.runtime = rt.detect_runtime() or ""
        self.flight = rt.preflight(self.runtime or None)

        host_cores = os.cpu_count() or 4
        self.host_cores = min(host_cores, self.flight.cores or host_cores)
        # Not capped: the estimate beside the spinner shows what a choice
        # costs, and the choice stays with whoever is running it.
        self.max_cores = self.host_cores

        self.input_dir = tk.StringVar()
        self.input_type = tk.StringVar(value="bids")
        self.output_dir = tk.StringVar()
        self.external = tk.StringVar()
        # Default to what Docker's memory actually allows, not to every core:
        # more concurrent subjects than memory supports get OOM-killed hours in.
        # starts at what fits comfortably; the spinner goes to every core
        self.nprocs = tk.IntVar(value=max(1, min(self.flight.parallel,
                                                 self.host_cores)))

        self._build()
        self._report_preflight()
        self._drain_job = self.after(100, self._drain)

    # ---------------------------------------------------------------- layout

    def _build(self):
        row = 0

        ttk.Label(self, text=APP_TITLE, font=("TkDefaultFont", 16, "bold"),
                  anchor="center").grid(row=row, column=0, columnspan=3,
                                        sticky="ew", pady=(0, 16))
        row += 1

        row = self._entry(row, "Scans folder", self.input_dir,
                          command=self._pick_scans)

        ttk.Label(self, text="Folder layout").grid(row=row, column=0,
                                                   sticky="w", pady=5)
        self.layout_box = ttk.Combobox(self, textvariable=self.input_type,
                                       values=["bids", "flat"],
                                       state="readonly", width=18)
        self.layout_box.grid(row=row, column=1, sticky="w", pady=5)
        row += 1

        row = self._entry(row, "Output folder", self.output_dir)

        row = self._entry(row, "Existing recon-all  (optional)", self.external)
        row = self._hint(row, "Must be FreeSurfer 7.x output - "
                              "earlier versions are not compatible")

        ttk.Label(self, text="Subjects in parallel").grid(row=row, column=0,
                                               sticky="w", pady=5)
        cores = ttk.Frame(self)
        cores.grid(row=row, column=1, sticky="w", pady=5)
        ttk.Spinbox(cores, from_=1, to=self.max_cores, width=6,
                    textvariable=self.nprocs).pack(side="left")
        # the RAM section is written by _report_preflight with the right
        # figure already; this only keeps it in step afterwards
        self.nprocs.trace_add("write", lambda *_: self._update_estimate())
        row += 1

        ttk.Separator(self).grid(row=row, column=0, columnspan=3,
                                 sticky="ew", pady=14)
        row += 1

        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.run_btn = ttk.Button(bar, text="Run", command=self.on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.on_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        row += 1

        ttk.Label(self, text="Messages", anchor="center",
                  font=("TkDefaultFont", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        row += 1

        self.log = tk.Text(self, height=16, wrap="word", background="#111",
                           foreground="#ddd", insertbackground="#ddd",
                           font=("TkFixedFont", 9))
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.log.yview)
        scroll.grid(row=row, column=3, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("head", foreground="#6cf",
                               font=("TkFixedFont", 9, "bold"))
        self.log.tag_configure("fail", foreground="#f66",
                               font=("TkFixedFont", 9, "bold"))
        self.log.tag_configure("good", foreground="#6c6",
                               font=("TkFixedFont", 9, "bold"))
        self.rowconfigure(row, weight=1)

    def _ram_text(self, n: int) -> str:
        want = rt.estimate_gb(n)
        host = self.flight.host_gb
        total = f" of {host:.0f} GB total" if host else ""
        line = (f"{n} x {rt.GB_PER_SUBJECT:g} GB per subject = "
                f"{want:.0f} GB{total}.")
        if host and want > host:
            line += f"\nThat is more RAM than this computer has."
        return line + '\nChange "Subjects in parallel" above to use more or less.'

    def _update_estimate(self):
        """Keep the RAM figure in the Messages panel following the spinner."""
        try:
            n = int(self.nprocs.get())
        except (tk.TclError, ValueError):
            return                                # mid-typing in the spinbox
        self._replace_section("ram", self._ram_text(n))

    def _entry(self, row, label, var, command=None):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(self, textvariable=var).grid(row=row, column=1,
                                               sticky="ew", pady=5)
        ttk.Button(self, text="Browse", width=8,
                   command=command or (lambda: self._pick_dir(var))).grid(
            row=row, column=2, sticky="w", padx=(6, 0))
        return row + 1

    def _hint(self, row, text):
        ttk.Label(self, text=text, foreground=HINT,
                  font=("TkDefaultFont", 8)).grid(
            row=row, column=1, columnspan=2, sticky="w", pady=(0, 6))
        return row + 1

    def _ask_dir(self, title: str) -> str:
        """Native desktop picker where one exists, Tk's own dialog otherwise."""
        path = rt.native_directory_dialog(title)
        if path is None:
            path = filedialog.askdirectory(title=title)
        return path or ""

    def _pick_dir(self, var):
        if path := self._ask_dir("Choose a folder"):
            var.set(path)

    # ------------------------------------------------- dataset autodetection

    def _pick_scans(self):
        path = self._ask_dir("Choose the folder with your scans")
        if not path:
            return
        self.input_dir.set(path)
        self._scan(path)

    def _scan(self, path: str):
        """Inspect on a worker thread: rglob over a big tree, possibly on a
        network share, must not freeze the window."""
        self._section("Scans folder", f"Inspecting {path}")

        def worker():
            self.lines.put(("REPORT", rt.scan_dataset(Path(path))))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_report(self, report: rt.DatasetReport):
        self.report = report

        if report.problem:
            self._write(report.problem + "\n")
            return

        for line in report.lines:
            self._write(line + "\n")

        self.input_type.set(report.layout)      # auto-select, still editable

    # ------------------------------------------------------------- preflight

    def _report_preflight(self):
        f = self.flight

        if not f.ok:
            self._section("Software", "\n".join(f.errors))
            self.run_btn.state(["disabled"])
            return

        _, message = rt.image_status(self.runtime)
        self._section("Software", message)
        self._section("RAM usage", self._ram_text(self.nprocs.get()),
                      mark="ram")

        for w in f.warnings:
            self._section("Note", w)

    # -------------------------------------------------------------- commands

    def _validate(self) -> str | None:
        if not self.flight.ok:
            return "Docker is not usable yet. See the message in the log."
        if not self.input_dir.get():
            return "Choose the folder holding your scans."
        if self.report is None or self.report.problem:
            return "No usable scans found in that folder."
        if not self.output_dir.get():
            return "Choose an output folder."
        if self.external.get() and not Path(self.external.get()).is_dir():
            return "The existing recon-all folder does not exist."
        if not 1 <= self.nprocs.get() <= self.max_cores:
            return f"CPU cores must be between 1 and {self.max_cores}."
        return None

    # --------------------------------------------------------------- actions

    def on_run(self):
        if problem := self._validate():
            messagebox.showwarning(APP_TITLE, problem)
            return

        present, _ = rt.image_status(self.runtime)

        if rt.container_running(self.runtime):
            messagebox.showwarning(
                APP_TITLE,
                "A run is already going in the background.\n\n"
                "Wait for it to finish, or press Stop, before starting another.")
            return

        if not messagebox.askyesno(
            APP_TITLE,
            "Keep this window open or minimized. The preprocessing logs will "
            "be shown in the Messages panel.\n\n"
            "If the window is closed the pipeline will stop, the same way as "
            "if the computer is turned off. If the pipeline is re-run with "
            "the same entries it will pick up from where it stopped.\n\n"
            "When it finishes, a message will show in the Messages panel.\n\n"
            "Start now?",
        ):
            return

        # What this core count wants, and the most the computer can spare.
        want = rt.estimate_gb(self.nprocs.get())
        ceiling = (rt.host_safe_gb(self.flight.host_gb)
                   if self.flight.host_gb else want)
        target = min(want, ceiling)

        # Docker Desktop's memory limit lives in a file this user owns, so if
        # it is too small for the chosen cores, raise it for the run instead of
        # asking anyone to go and change a slider. Put it back afterwards.
        self._restore_mem = None
        have = self.flight.memory_gb or 0
        needed_setting = target + 2.0            # room for the VM itself
        current = rt.read_memory_setting()

        steps = []
        if current and have < target and needed_setting > (current[2] / 1024):
            self._restore_mem = current[2] / 1024
            steps.append((
                f"Giving Docker {needed_setting:.0f} GB for this run",
                lambda gb=needed_setting: (rt.write_memory_setting(gb)
                                           and rt.restart_desktop())))

        cap = target
        budget = target

        if not present:
            steps.append(("Downloading the container",
                          rt.pull_command(self.runtime)))
        steps.append((
            "Pipeline run",
            rt.build_command(
                runtime=self.runtime,
                input_path=Path(self.input_dir.get()),
                input_type=self.input_type.get(),
                output_dir=Path(self.output_dir.get()),
                external_dir=Path(self.external.get()) if self.external.get() else None,
                n_procs=self.nprocs.get(),
                pipeline_flags=PIPELINE_FLAGS,
                subjects=self.report.subjects,
                memory_cap_gb=cap,
                scheduler_budget_gb=budget,
            )))
        self._spawn(steps)

    def on_stop(self):
        if not (self.proc and self.proc.poll() is None):
            return
        self.stopping = True
        self._write("\nStopping.\n")

        # "docker run" is only a client: the container lives in the daemon and
        # keeps computing if the client is killed, so the daemon must be asked.
        def worker():
            rt.stop_container(self.runtime)
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

        threading.Thread(target=worker, daemon=True).start()

    def _spawn(self, steps):
        self.stopping = False
        self.run_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])

        # Read on the main thread: Tk variables must not be touched from the
        # worker, and doing so raises "main thread is not in main loop".
        out_dir = Path(self.output_dir.get())

        def worker():
            code = 0
            tail: deque[str] = deque(maxlen=60)
            for label, cmd in steps:
                # A step can be a command to run, or something to do first -
                # such as giving Docker more memory, which is not a command.
                if callable(cmd):
                    self.lines.put(("SECTION", label,
                                    "This takes a minute while Docker "
                                    "restarts.\n"))
                    if cmd():
                        self.lines.put("Done.\n")
                        continue
                    self.lines.put("Could not change Docker's memory.\n")
                    code = 1
                    break

                self.lines.put(("SECTION", label,
                                "$ " + " ".join(str(c) for c in cmd) + "\n"))
                try:
                    self.proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, **rt.no_console())
                    assert self.proc.stdout is not None
                    for line in self.proc.stdout:
                        tail.append(line)
                        self.lines.put(line)
                    code = self.proc.wait()
                except FileNotFoundError:
                    self.lines.put(f"\n{cmd[0]} is not installed.\n")
                    code = 127
                except Exception as exc:                  # noqa: BLE001
                    self.lines.put(f"\n{exc}\n")
                    code = 1

                if code != 0:
                    break

            # Only borrowed for the run: put the original setting back. The
            # file alone is enough - it applies at Docker's next restart, so
            # nothing is disrupted now.
            if getattr(self, "_restore_mem", None):
                rt.write_memory_setting(self._restore_mem)
                self.lines.put(f"\nDocker's memory setting put back to "
                               f"{self._restore_mem:.0f} GB.\n")
                self._restore_mem = None

            if code == 0:
                self.lines.put(("GOOD", "Finished",
                                rt.summarise_outputs(out_dir)))
            elif self.stopping:
                self.lines.put(("SECTION", "Stopped",
                                "The run was stopped. Press Run again to carry "
                                "on from where it stopped."))
            else:
                self.lines.put(("FAIL", "Pipeline failed",
                                rt.diagnose_failure(self.runtime, code,
                                                    "".join(tail))))
            self.lines.put(None)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        """Closing the window stops the run, as the start dialog promises.
        Without this the container would keep going invisibly."""
        if getattr(self, "_drain_job", None):
            self.after_cancel(self._drain_job)
            self._drain_job = None
        if self.proc and self.proc.poll() is None:
            rt.stop_container(self.runtime, grace=3)
            try:
                self.proc.terminate()
            except Exception:                     # noqa: BLE001
                pass
        self.root.destroy()

    # -------------------------------------------------------------- log pump

    def _write(self, text: str):
        self.log.insert("end", text)
        self.log.see("end")

    def _section(self, title: str, body: str, tag: str = "head",
                 mark: str | None = None):
        """One labelled block, blank line between blocks.

        `mark` brackets the body with Text marks so it can be rewritten later
        in place - used by the RAM figure, which has to follow the spinner.
        """
        if self.log.index("end-1c") != "1.0":
            self.log.insert("end", "\n")
        self.log.insert("end", f"- {title}\n", tag)

        start = self.log.index("end-1c")
        self.log.insert("end", body.rstrip() + "\n")

        if mark:
            # One mark for where the body begins, plus how many lines it takes.
            # A second mark cannot express this: right gravity makes it travel
            # with every later append, left gravity makes the range collapse.
            self.log.mark_set(mark, start)
            self.log.mark_gravity(mark, "left")
            self._marked[mark] = body.rstrip().count("\n") + 1

        self.log.see("end")

    def _replace_section(self, mark: str, body: str) -> bool:
        """Rewrite a marked block, leaving everything around it alone."""
        lines = self._marked.get(mark)
        if lines is None:
            return False
        try:
            self.log.delete(mark, f"{mark} + {lines} lines")
            self.log.insert(mark, body.rstrip() + "\n")
            self._marked[mark] = body.rstrip().count("\n") + 1
            return True
        except tk.TclError:
            return False

    def _drain(self):
        try:
            while True:
                item = self.lines.get_nowait()

                if item is None:
                    self.run_btn.state(["!disabled"])
                    self.stop_btn.state(["disabled"])
                    continue

                if isinstance(item, tuple) and item[0] == "REPORT":
                    self._apply_report(item[1])
                    continue

                if isinstance(item, tuple) and item[0] == "SECTION":
                    self._section(item[1], item[2])
                    continue

                if isinstance(item, tuple) and item[0] == "FAIL":
                    self._section(item[1], item[2], tag="fail")
                    continue

                if isinstance(item, tuple) and item[0] == "GOOD":
                    self._section(item[1], item[2], tag="good")
                    continue

                self._write(item)
        except queue.Empty:
            pass
        self._drain_job = self.after(100, self._drain)


def main():
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(780, 700)
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = Launcher(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
