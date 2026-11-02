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
import time
from collections import deque
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import runtime as rt

APP_TITLE = "ENIGMA multi-phenotype morphometry"

# The protocol. Every site runs these unchanged - that is what makes the
# results poolable.
# The steps a site may, but should not, switch off. Order matches slurm.sh;
# recon-all, subcorticalshape, subseg and segpve are not offered because the
# rest depends on them.
CORTICAL_STEPS = ["after2.curvs", "after2.mantle", "after2.shfd", "after2.lgi",
                  "after2.gwc", "after2.myelin", "mind"]
SUBCORTICAL_STEPS = ["scmind"]
STEP_CHOICES = CORTICAL_STEPS + SUBCORTICAL_STEPS

# Everything after the two measure lists; identical at every site.
PIPELINE_FLAGS_REST = [
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
    "--zip-contents", "all",
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
        self._scanned_path: str | None = None     # which folder self.report is about
        self._orphan_job = None

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
        # all on by default: the protocol has to be the same at every site
        self.steps = {name: tk.BooleanVar(value=True) for name in STEP_CHOICES}

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
                          command=self._pick_scans,
                          on_commit=self._scan_if_changed)

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

        ttk.Label(self, text="Preprocessing steps").grid(row=row, column=0,
                                                        sticky="nw", pady=5)
        boxes = ttk.Frame(self)
        boxes.grid(row=row, column=1, columnspan=2, sticky="w", pady=5)
        per_row = 4
        for i, name in enumerate(STEP_CHOICES):
            ttk.Checkbutton(boxes, text=name, variable=self.steps[name]).grid(
                row=i // per_row, column=i % per_row, sticky="w", padx=(0, 14))
        row += 1
        row = self._hint(row, "Please do not change this.")

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
        if host:
            _, cap = rt.run_plan(n, host)
            if cap + 0.5 < want:
                # the figure Run will actually use, so nobody is surprised later
                fits = int(cap // rt.GB_PER_SUBJECT)
                line += (f"\nThis computer can give the run up to {cap:.0f} GB"
                         + (f", enough for {fits} at a time." if fits >= 1
                            else f", less than the {rt.GB_PER_SUBJECT:g} GB "
                                 f"one subject is estimated to need. The run "
                                 f"will still be attempted with it."))
        return line + '\nChange "Subjects in parallel" above to use more or less.'

    def _pipeline_flags(self) -> list[str]:
        """The full protocol, with whichever steps are ticked. Main thread
        only: it reads Tk variables."""
        cortical = ["recon-all"] + [s for s in CORTICAL_STEPS
                                    if self.steps[s].get()]
        subcortical = (["subcorticalshape"]
                       + [s for s in SUBCORTICAL_STEPS if self.steps[s].get()]
                       + ["subseg", "segpve"])
        return (["--cortical-measures", *cortical,
                 "--subcortical-measures", *subcortical]
                + PIPELINE_FLAGS_REST)

    def _update_estimate(self):
        """Keep the RAM figure in the Messages panel following the spinner."""
        try:
            n = int(self.nprocs.get())
        except (tk.TclError, ValueError):
            return                                # mid-typing in the spinbox
        self._replace_section("ram", self._ram_text(n))

    def _entry(self, row, label, var, command=None, on_commit=None):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(self, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        if on_commit:
            # a typed or pasted path is taken when the field is left or Enter
            # is pressed, so it gets inspected like a browsed one
            entry.bind("<FocusOut>", lambda _e: on_commit())
            entry.bind("<Return>", lambda _e: on_commit())
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
        self._scanned_path = path
        self.report = None                        # stale until the report lands
        self._section("Scans folder", f"Inspecting {path}")

        def worker():
            self.lines.put(("REPORT", path, rt.scan_dataset(Path(path))))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_if_changed(self):
        """A path typed into the field is inspected too, not only a browsed one."""
        path = self.input_dir.get().strip()
        if path and path != self._scanned_path:
            self._scan(path)

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

        # Not even one subject can have what it needs: say so at once, rather
        # than letting a run start and die hours in.
        if f.host_gb:
            _, cap_one = rt.run_plan(1, f.host_gb)
            if cap_one < rt.GB_PER_SUBJECT:
                self._section(
                    "Note",
                    f"This computer can give the pipeline at most {cap_one:.0f} GB; "
                    f"one subject is estimated to need about "
                    f"{rt.GB_PER_SUBJECT:g} GB. Runs will still be attempted "
                    f"with what there is, one subject at a time. If a step "
                    f"runs out of memory the run stops and says so. That limit "
                    f"comes from the computer's total RAM ({f.host_gb:.0f} GB), "
                    f"not from any setting.")

        # A run left going by a session that was closed without stopping it.
        if rt.container_running(self.runtime):
            self._section(
                "Note",
                "A run started earlier is still going in the background - "
                "probably from a session that was closed without stopping it. "
                "Press Stop to end it, or wait for it to finish; Run is not "
                "available until then.")
            self.stop_btn.state(["!disabled"])
            self.run_btn.state(["disabled"])
            self._watch_orphan()

    def _watch_orphan(self):
        """Give Run back once a background run from an earlier session ends."""
        if rt.container_running(self.runtime):
            self._orphan_job = self.after(15000, self._watch_orphan)
            return
        self._orphan_job = None
        self.run_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        self._section("Note", "The background run has ended. Press Run to "
                              "carry on from where it stopped.")

    # -------------------------------------------------------------- commands

    def _validate(self) -> str | None:
        if not self.flight.ok:
            return "Docker is not usable yet. See the message in the log."
        path = self.input_dir.get().strip()
        if not path:
            return "Choose the folder holding your scans."
        if path != self._scanned_path or self.report is None:
            # typed after the last inspection, or still being inspected: look
            # now rather than run with another folder's counts
            self._scanned_path = path
            self._section("Scans folder", f"Inspecting {path}")
            self._apply_report(rt.scan_dataset(Path(path)))
        if self.report is None or self.report.problem:
            return "No usable scans found in that folder."
        if self.report.blocking:
            return self.report.blocking
        if not self.output_dir.get():
            return "Choose an output folder."
        if self.external.get() and not Path(self.external.get()).is_dir():
            return "The existing recon-all folder does not exist."
        try:
            n = int(self.nprocs.get())
        except (tk.TclError, ValueError):
            n = 0
        if not 1 <= n <= self.max_cores:
            return (f"Subjects in parallel must be a whole number between 1 "
                    f"and {self.max_cores}.")
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

        # What this many subjects wants, and the most Docker may sanely be set
        # to on this computer - the same arithmetic the RAM text shows.
        n_procs = int(self.nprocs.get())
        needed_setting, target = rt.run_plan(n_procs, self.flight.host_gb)

        # Read now, on the main thread; the worker must not touch Tk variables.
        args = dict(
            runtime=self.runtime,
            input_path=Path(self.input_dir.get()),
            input_type=self.input_type.get(),
            output_dir=Path(self.output_dir.get()),
            external_dir=Path(self.external.get()) if self.external.get() else None,
            n_procs=n_procs,
            pipeline_flags=self._pipeline_flags(),
            # what the pipeline actually iterates over - one per session in
            # BIDS, one per file in flat - so the whole dataset is one archive
            subjects=self.report.units,
        )

        steps = []

        # Docker's memory limit lives in a file this user owns, so raise it for
        # the run rather than asking anyone to move a slider. Best effort only.
        have = self.flight.memory_gb or 0
        wsl = self.flight.wsl
        if have < target and rt.can_start_with_gb(needed_setting):
            steps.append((
                f"Giving Docker {needed_setting:.0f} GB for this run",
                lambda gb=needed_setting: rt.ensure_memory(gb, wsl=wsl),
                # on WSL 2 nothing restarts: the step only says what to do
                None if wsl else "This takes a minute while Docker restarts."))

        start = rt.start_hint(self.flight.flavour)
        steps.append(("Checking Docker is running",
                      lambda: (True, "Docker is running.")
                      if rt.ensure_running(self.runtime)
                      else (False, "Docker is not running and could not be "
                                   f"started. {start} Then press Run again.")))

        if not present:
            steps.append(("Downloading the container",
                          rt.pull_command(self.runtime)))

        # Built last, and lazily: only now is it known how much memory Docker
        # really ended up with, which may be less than was asked for.
        def pipeline_command():
            flight = rt.preflight(args["runtime"])
            usable, _, _ = rt.memory_plan(flight.memory_gb, self.host_cores,
                                          flight.host_gb)
            cap = min(target, usable) if usable else target
            # The scheduler is deliberately told less than the hard cap.
            # Observed: hypothalamic_subunits_node declares mem_gb=8.0 and was
            # still killed by signal 9, so declared estimates undershoot real
            # use. Leaving a margin between what the scheduler packs to and
            # what Docker enforces gives that overshoot somewhere to go.
            return rt.build_command(memory_cap_gb=cap,
                                    scheduler_budget_gb=round(cap * 0.85, 1),
                                    **args)

        steps.append(("Pipeline run", pipeline_command))
        self._spawn(steps)

    def on_stop(self):
        if not (self.proc and self.proc.poll() is None):
            # nothing of ours is running, but a container from an earlier
            # session may be - stop that instead
            self._write("\nStopping the background run.\n")
            threading.Thread(target=lambda: rt.stop_container(self.runtime),
                             daemon=True).start()
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
        n_units = self.report.units if self.report else None
        unit_word = ("subject-sessions"
                     if self.report and self.report.longitudinal else "subjects")
        flavour = self.flight.flavour
        # So crash reports written from now on can be told apart from ones
        # left by earlier attempts.
        started_at = time.time()

        def worker():
            code = 0
            failed_step = None
            tail: deque[str] = deque(maxlen=60)
            for step in steps:
                label, cmd = step[0], step[1]
                note = step[2] if len(step) > 2 else None
                # A step is either a command, or something to do first that
                # produces one. Preparation steps never fail the run: if they
                # cannot do what they wanted, the run continues with whatever
                # the machine is actually able to give.
                if callable(cmd):
                    if note:
                        # only a step that keeps the user waiting announces
                        # itself first; a factory just yields its command
                        self.lines.put(("SECTION", label, note + "\n"))
                    result = cmd()

                    if isinstance(result, (list, tuple)) and result and \
                            isinstance(result[0], str):
                        cmd = list(result)        # it built the command
                    else:
                        ok, text = result
                        if note:
                            self.lines.put(text.rstrip() + "\n")
                        else:
                            self.lines.put(("SECTION", label, text.rstrip() + "\n"))
                        continue

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
                    failed_step = label
                    break

            # The setting is left where it is. It is bounded by what is safe
            # for this computer, and putting it back would mean another Docker
            # restart now and again at the start of the next run.
            if code == 0:
                self.lines.put(("GOOD", "Finished",
                                rt.summarise_outputs(out_dir, n_units,
                                                     started_at, unit_word)))
            elif self.stopping:
                self.lines.put(("SECTION", "Stopped",
                                "The run was stopped. Press Run again to carry "
                                "on from where it stopped."))
            else:
                self.lines.put(("FAIL", "Pipeline failed",
                                rt.diagnose_failure(self.runtime, code,
                                                    "".join(tail), out_dir,
                                                    started_at, failed_step,
                                                    flavour)))
            self.lines.put(None)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        """Closing the window stops the run, as the start dialog promises.
        Without this the container would keep going invisibly."""
        for attr in ("_drain_job", "_orphan_job"):
            job = getattr(self, attr, None)
            if job:
                self.after_cancel(job)
                setattr(self, attr, None)
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
                    # only the folder currently in the field, and only if Run
                    # has not already inspected it itself in the meantime
                    if item[1] == self._scanned_path and self.report is None:
                        self._apply_report(item[2])
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
    root.minsize(780, 760)
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    app = Launcher(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
