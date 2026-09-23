#!/usr/bin/env python3
# ---------------------------------------------------------------------------
#  Container runtime + dataset inspection for the ENIGMA morphometry launcher.
#
#  Docker only. It is the one runtime that runs natively on Linux, macOS and
#  Windows, so the desktop app has a single code path on every platform.
#  Podman is accepted as a drop-in because its CLI is identical.
#
#  (Apptainer is faster and needs no daemon, but exists on Linux only. The
#  cluster version uses it; the desktop app deliberately does not.)
#
#  Paths inside the container are FIXED (/input, /output, /external) rather
#  than mirroring the host. That is what makes Windows work at all: "C:\data"
#  is not a path a Linux container can use on its own side of the mount.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

IMAGE = "csleo/morphometry2:fs-7.4.1"

# The container is named so it can be stopped for real. "docker run" is only a
# client: killing it leaves the container computing inside the daemon, so the
# Stop button and closing the window both have to ask the daemon to stop it.
# A fixed name also means a second run cannot start while one is still going,
# which would otherwise have two containers writing one output folder.
CONTAINER_NAME = "enigma-morphometry"

# The image is linux/amd64: FreeSurfer 7.4.1 ships no Linux arm64 build, so
# there is no native image to build for Apple Silicon. Stating the platform
# explicitly makes Docker emulate instead of refusing.
IMAGE_PLATFORM = "linux/amd64"

C_IN, C_OUT, C_EXT = "/input", "/output", "/external"

# Where the finished archive is written, inside the output folder.
ZIP_SUBDIR = "zips"

# --subjects-per-tar is set to the number of subjects found, so the whole
# dataset lands in one archive.
#
# The pipeline names each archive after the subjects in it, so that name is
# fixed the moment the first run writes dataset.csv. Adding or removing scans
# afterwards would rename the archive and orphan what was already written, and
# the pipeline refuses rather than doing that silently. Nothing else about a
# run is affected: the number of subjects in parallel may differ every time.

CONTAINER_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "OMP_NUM_THREADS": "1",
    "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS": "1",
}

# Memory to allow per subject running at once. Sourced, not guessed:
#
#   recon-all alone   4 GB   FreeSurfer SystemRequirements: "four cores and
#                            16GB of memory to process four subjects"
#                     5 GB   fMRIPrep annotates FreeSurfer nodes at 5 GB,
#                            "4GB per subject ... 5GB to have a certain margin"
#   whole per-subject 8 GB   fMRIPrep FAQ: "About 8GB of memory should be
#                            available for a single subject preprocessing run"
#
# 4 GB (morphometry.py's own default) covers recon-all on its own. It does not
# cover MRIQC, which runs concurrently here: nipreps/mriqc#1437 documents that
# the SynthStrip node declares mem_gb=0.25 while actually using 4-6 GB, so the
# scheduler happily starts three or four at once. That undercount cannot be
# fixed from outside the container, so the headroom has to absorb it.
GB_PER_SUBJECT = 8.0

# "docker run --platform" needs 20.10 (API 1.41). We only send that flag when
# the host architecture differs from the image, so older Docker still works on
# an ordinary x86-64 machine.
MIN_DOCKER_FOR_PLATFORM = (20, 10)

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
IS_ARM = platform.machine().lower() in ("arm64", "aarch64")

INSTALL_HINT = {
    "Linux": ("Install Docker Engine:  https://docs.docker.com/engine/install/\n"
              "  After installing, add yourself to the docker group:\n"
              "      sudo usermod -aG docker $USER\n"
              "  then log out and back in."),
    "Darwin": "Install Docker Desktop:  https://www.docker.com/products/docker-desktop",
    "Windows": "Install Docker Desktop:  https://www.docker.com/products/docker-desktop",
}


def host_memory_gb() -> float | None:
    """Physical RAM of the computer itself, not of Docker's virtual machine."""
    try:
        if IS_LINUX:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / 1024 / 1024
        elif IS_MAC:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip()) / 1024**3
        elif IS_WINDOWS:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / 1024**3
    except Exception:                             # noqa: BLE001
        return None
    return None


def host_free_gb() -> float | None:
    """RAM actually free right now, not the machine's total.

    What matters is what is spare at this moment, not a fraction of the total:
    the same computer has very different headroom with a browser open than
    without. Asking Docker for more than this does not make the run faster,
    it makes the kernel kill Docker's virtual machine.
    """
    try:
        if IS_LINUX:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024 / 1024
    except Exception:                             # noqa: BLE001
        pass
    return None


# Left free for the desktop so it stays responsive while a run is going.
DESKTOP_RESERVE_GB = 4.0


def host_safe_gb(host_gb: float) -> float:
    """Most the container may use without putting the computer at risk.

    Based on what is free now where that can be read, so a machine with 32 GB
    spare may use close to 32 GB - and the same machine with only 10 GB spare
    is held to that instead.
    """
    free = host_free_gb()
    if free:
        return max(4.0, min(host_gb * 0.85, free - DESKTOP_RESERVE_GB))
    # Cannot read what is free: fall back to a share of the total.
    return max(4.0, min(host_gb * 0.60, host_gb - 12.0))


def parse_version(text: str | None) -> tuple[int, int] | None:
    """('24.0.7') -> (24, 0). None when it cannot be read."""
    if not text:
        return None
    nums = re.findall(r"\d+", text)
    if len(nums) < 2:
        return None
    return int(nums[0]), int(nums[1])


def platform_args() -> list[str]:
    """--platform is only meaningful when the host cannot run the image
    natively. Sending it on x86-64 gains nothing and breaks Docker < 20.10."""
    return ["--platform", IMAGE_PLATFORM] if IS_ARM else []


def no_console() -> dict:
    """Keep a console window from flashing up on Windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def detect_runtime() -> str | None:
    for name in ("docker", "podman"):     # identical CLI; podman is a drop-in
        if shutil.which(name):
            return name
    return None


def native_directory_dialog(title: str) -> str | None:
    """Ask the desktop for a folder, rather than letting Tk draw the dialog.

    tkinter has no GTK binding, so on Linux it renders its own file chooser in
    Tcl. Under GNOME that dialog's "Directory" dropdown opens and closes again
    immediately, which makes it nearly unusable. zenity/kdialog hand back the
    real desktop picker.

    macOS and Windows already get a native dialog from Tk, so they skip this.

    Returns the chosen path, "" if the user cancelled, or None if no native
    helper exists (caller should fall back to Tk).
    """
    if not IS_LINUX:
        return None

    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", "--modal",
               "--title", title]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", str(Path.home())]
    else:
        return None

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception:                             # noqa: BLE001
        return None                               # fall back to Tk

    if out.returncode != 0:                       # cancelled, or the tool failed
        return "" if out.returncode == 1 else None

    return out.stdout.strip()


# =========================================================================
#  Dataset inspection
#
#  Mirrors what the pipeline itself does in utils/dataset.py, so the counts
#  shown here are the counts it will actually process:
#    flat  - every NIfTI at the top level is its own subject
#    bids  - one subject per sub-*, one row per session
# =========================================================================

NIFTI_SUFFIXES = (".nii", ".nii.gz")


def _niftis(folder: Path, pattern: str) -> list[Path]:
    hits: list[Path] = []
    for ext in NIFTI_SUFFIXES:
        hits += list(folder.rglob(f"{pattern}{ext}"))
    return hits


@dataclass
class DatasetReport:
    layout: str = "unknown"        # bids | flat | unknown
    subjects: int = 0
    sessions: int = 0
    t1: int = 0
    t2: int = 0
    flair: int = 0
    longitudinal: bool = False
    multi_run: bool = False
    skipped: int = 0
    problem: str | None = None
    lines: list[str] = field(default_factory=list)


def scan_dataset(root: Path) -> DatasetReport:
    """Work out whether this is BIDS or flat, and count what is in it.

    Called on a worker thread: rglob over a big tree on a network share is
    not something the GUI should block on.
    """
    r = DatasetReport()
    root = Path(root)

    if not root.is_dir():
        r.problem = f"{root} is not a folder."
        return r

    try:
        entries = list(root.iterdir())
    except PermissionError:
        r.problem = f"Cannot read {root} (permission denied)."
        return r

    sub_dirs = sorted(p for p in entries if p.is_dir() and p.name.startswith("sub-"))
    top_niftis = sorted(p for p in entries
                        if p.is_file() and p.name.endswith(NIFTI_SUFFIXES))

    # -------------------------------------------------- BIDS
    if sub_dirs:
        r.layout = "bids"
        sessions_per_subject: list[int] = []
        t1_per_session: list[int] = []

        for sub in sub_dirs:
            ses_dirs = sorted(d for d in sub.iterdir()
                              if d.is_dir() and d.name.startswith("ses-"))
            units = ses_dirs or [sub]

            has_t1_here = False
            for unit in units:
                n_t1 = len(_niftis(unit, "*_T1w"))
                if n_t1:
                    # get_T1_niftis_bids skips a session with no T1w, so it
                    # produces no row and must not be counted here either
                    has_t1_here = True
                    t1_per_session.append(n_t1)
                    r.sessions += 1
                r.t1 += n_t1
                r.t2 += len(_niftis(unit, "*_T2w"))
                r.flair += len(_niftis(unit, "*_FLAIR"))

            if has_t1_here:
                r.subjects += 1
                sessions_per_subject.append(len(ses_dirs))
            else:
                r.skipped += 1

        r.longitudinal = any(n > 1 for n in sessions_per_subject)
        r.multi_run = any(n > 1 for n in t1_per_session)

        if r.t1 == 0:
            r.problem = ("Found sub-* folders but no *_T1w images. "
                         "The pipeline needs T1w to run.")
            return r

        r.lines = [
            "Detected layout: BIDS",
            f"  subjects        : {r.subjects}",
            f"  sessions        : {r.sessions}"
            + ("  (longitudinal)" if r.longitudinal else ""),
            f"  T1w scans       : {r.t1}"
            + ("  (some sessions have more than one run)" if r.multi_run else ""),
            f"  T2w scans       : {r.t2}" + ("" if r.t2 else "  (none)"),
            f"  FLAIR scans     : {r.flair}" + ("" if r.flair else "  (none)"),
        ]
        if r.t1 != r.subjects:
            r.lines.append(
                f"  note            : {r.t1} scans across {r.subjects} subjects")
        if r.skipped:
            r.lines.append(
                f"  SKIPPED         : {r.skipped} sub-* folder(s) have no T1w and "
                f"will not be processed")
        return r

    # -------------------------------------------------- flat
    if top_niftis:
        r.layout = "flat"
        r.t1 = len(top_niftis)
        r.subjects = len(top_niftis)      # flat: one file == one subject
        r.sessions = r.subjects
        r.lines = [
            "Detected layout: flat",
            f"  NIfTI files     : {r.t1}",
            f"  subjects        : {r.subjects}   (flat treats each file as one subject)",
            "  T2w / FLAIR     : not used in the flat layout",
        ]
        return r

    r.problem = (f"No sub-* folders and no .nii/.nii.gz files directly in {root.name}. "
                 "Point at the folder that holds the scans, or the BIDS root.")
    return r


# =========================================================================
#  Docker
# =========================================================================

@dataclass
class Preflight:
    runtime: str | None = None
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cores: int | None = None
    memory_gb: float | None = None
    storage_dir: str | None = None
    server_version: str | None = None
    host_gb: float | None = None
    parallel: int = 1
    limited_by: str = ""
    summary: str = ""


def preflight(runtime: str | None) -> Preflight:
    """Is Docker installed, running, and usable by this user?

    Three distinct failures, each with its own fix, because "cannot connect
    to the Docker daemon" tells the user nothing about which one they hit.
    """
    r = Preflight(runtime=runtime)

    # ---- not installed
    if runtime is None:
        r.ok = False
        r.errors.append(
            "Docker was not found on this computer.\n\n"
            + INSTALL_HINT.get(platform.system(), INSTALL_HINT["Linux"])
            + "\n\nThen close and reopen this app."
        )
        return r

    try:
        out = subprocess.run(
            [runtime, "info", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=25, **no_console(),
        )
    except FileNotFoundError:
        r.ok = False
        r.errors.append(f"{runtime} was not found on this computer.")
        return r
    except subprocess.TimeoutExpired:
        r.ok = False
        r.errors.append(
            f"{runtime} did not respond within 25 seconds.\n"
            "It is probably still starting up - wait a moment, then close "
            "and reopen this app."
        )
        return r

    if out.returncode != 0:
        r.ok = False
        blob = ((out.stderr or "") + (out.stdout or "")).lower()

        # ---- installed, but this user may not talk to the daemon
        if "permission denied" in blob or "connect: permission denied" in blob:
            r.errors.append(
                f"{runtime} is installed but this user is not allowed to use it.\n\n"
                "  sudo usermod -aG docker $USER\n\n"
                "You must log out and back in for this to take effect "
                "(a reboot also works), then reopen this app."
            )
        # ---- installed, daemon down
        else:
            detail = (out.stderr or out.stdout).strip().splitlines()
            hint = detail[-1] if detail else "unknown error"
            if IS_LINUX:
                fix = "Start it with:  sudo systemctl start docker"
            else:
                fix = "Start Docker Desktop and wait for the whale icon to stop animating."
            r.errors.append(
                f"{runtime} is installed but not running.\n\n{fix}\n\n"
                "Once it is running, close and reopen this app.\n\n"
                f"({hint})"
            )
        return r

    # ---- usable
    try:
        info = json.loads(out.stdout)
        r.cores = info.get("NCPU") or None
        mem = info.get("MemTotal")
        r.memory_gb = round(mem / 1024**3, 1) if mem else None
        r.storage_dir = info.get("DockerRootDir") or None
        r.server_version = info.get("ServerVersion") or None
    except (json.JSONDecodeError, TypeError):
        pass

    # "docker info" exits 0 even when the engine is dead: it prints a complete
    # JSON document in which every server field is zero or empty, and puts the
    # real error on stderr. Trusting the exit code alone reports a broken
    # Docker as healthy, and then everything downstream is nonsense.
    if not r.server_version or not r.memory_gb:
        r.ok = False
        detail = (out.stderr or "").strip().splitlines()
        hint = next((line.strip() for line in detail if line.strip()), "")

        if "unable to start" in hint.lower() or "qemu" in (out.stderr or "").lower():
            r.errors.append(
                "Docker Desktop could not start.\n\n"
                "This usually means its memory setting is larger than the "
                "computer currently has free, so it is stopped as soon as it "
                "tries to start.\n\n"
                "Lower the memory in Docker Desktop > Settings > Resources, "
                "then close and reopen this app."
                + (f"\n\n({hint})" if hint else "")
            )
        else:
            fix = ("Start it with:  sudo systemctl start docker" if IS_LINUX
                   else "Start Docker Desktop and wait for it to finish starting.")
            r.errors.append(
                f"{runtime} is installed but its engine is not running.\n\n"
                f"{fix}\n\nOnce it is running, close and reopen this app."
                + (f"\n\n({hint})" if hint else "")
            )
        return r

    version = parse_version(r.server_version)
    if version and version < MIN_DOCKER_FOR_PLATFORM:
        pretty = ".".join(str(n) for n in MIN_DOCKER_FOR_PLATFORM)
        if IS_ARM:
            # no --platform means no way to ask for the amd64 image here
            r.ok = False
            r.errors.append(
                f"Docker {r.server_version} is too old for this computer. "
                f"Running the pipeline on this processor needs Docker {pretty} "
                f"or newer.\n\nPlease update Docker Desktop, then close and "
                f"reopen this app."
            )
            return r
        r.warnings.append(
            f"Docker {r.server_version} is older than {pretty}. It should still "
            f"work on this computer, but updating Docker is recommended."
        )

    host_gb = host_memory_gb()
    r.host_gb = host_gb

    # Deliberately no warning about free memory here. It is read at the moment
    # the window opens, so a merely busy machine looked like a configuration
    # problem, and the advice it gave pointed at a Docker setting that was not
    # the cause. The app raises Docker's memory for the run instead.

    r.parallel, r.limited_by, r.summary = describe_capacity(
        r.memory_gb, r.cores, host_gb)

    if IS_ARM and IS_MAC:
        r.warnings.append(
            "Apple Silicon detected. The image is linux/amd64 because FreeSurfer "
            "ships no Linux arm64 build, so it runs under emulation - correct "
            "results, but roughly 2-3x slower."
        )

    return r


def estimate_gb(n_procs: int) -> float:
    """Rough RAM the run will want with this many subjects at once."""
    return max(1, n_procs) * GB_PER_SUBJECT


def memory_plan(docker_gb: float | None, cores: int | None,
                host_gb: float | None = None
                ) -> tuple[float | None, float | None, int]:
    """(hard container cap, scheduler budget, subjects at once).

    Three separate numbers, because one alone is not enough:

    * the cap is given to "docker run --memory". Without it a runaway pipeline
      grows until the host kills Docker's whole virtual machine, taking the run
      and sometimes the desktop with it. With it, Docker kills only the
      container, which is recoverable and reports cleanly as exit 137.
    * the budget is given to the pipeline's own scheduler via --mem-gb. Left to
      itself it assumes 4 GB x CPU cores and happily schedules past whatever
      Docker actually has.
    * subjects-at-once follows from the budget, not from the core count.
    """
    cores = cores or 1
    if not docker_gb:
        return None, None, 1

    # the VM needs room for its own kernel and services
    cap = max(2.0, docker_gb - 2.0)

    # Docker may be configured to hand over more than the computer can spare.
    # Rather than telling anyone to go and change that, take only what is safe.
    if host_gb:
        cap = min(cap, host_safe_gb(host_gb))
    # The scheduler is told the true ceiling. It cannot be trusted to respect
    # it (see the SynthStrip undercount above), which is why the cap is also
    # enforced by Docker and why concurrency is limited separately.
    budget = round(cap, 1)
    parallel = max(1, min(int(cap // GB_PER_SUBJECT), cores))
    return cap, budget, parallel


def describe_capacity(memory_gb: float | None, cores: int | None,
                      host_gb: float | None = None) -> tuple[int, str, str]:
    """How many subjects run at once. Describes, it does not give orders."""
    cores = cores or 1

    if memory_gb is None:
        return 1, "unknown", (
            "Could not read how much memory Docker has, so 1 subject will be "
            "run at a time.")

    cap, _, parallel = memory_plan(memory_gb, cores, host_gb)
    limited_by = "memory" if int(cap // GB_PER_SUBJECT) <= cores else "CPU cores"

    total = f" of {host_gb:.0f} GB total" if host_gb else ""
    return parallel, limited_by, (
        f"{parallel} x {GB_PER_SUBJECT:g} GB per subject = "
        f"{parallel * GB_PER_SUBJECT:.0f} GB{total}.\n"
        f"Change \"Subjects in parallel\" above to use more or less.")


def image_status(runtime: str) -> tuple[bool, str]:
    """(present, message) - so the user knows whether the long download
    still has to happen."""
    try:
        out = subprocess.run(
            [runtime, "image", "inspect", IMAGE, "--format", "{{.Size}}"],
            capture_output=True, text=True, timeout=20, **no_console())
        if out.returncode == 0:
            try:
                gb = int(out.stdout.strip()) / 1024**3
                return True, (f"docker image {IMAGE} has already been "
                              f"downloaded ({gb:.1f} GB).")
            except ValueError:
                return True, f"docker image {IMAGE} has already been downloaded."
    except Exception:                             # noqa: BLE001
        pass
    return False, (f"docker image has not been downloaded yet, {IMAGE} will be "
                   f"fetched on the first run. This happens once.")



def pull_command(runtime: str) -> list[str]:
    return [runtime, "pull"] + platform_args() + [IMAGE]


def build_command(
    runtime: str,
    input_path: Path,
    input_type: str,
    output_dir: Path,
    external_dir: Path | None,
    n_procs: int,
    pipeline_flags: list[str],
    subjects: int,
    memory_cap_gb: float | None = None,
    scheduler_budget_gb: float | None = None,
) -> list[str]:
    """Assemble the full docker invocation.

    bids/flat map onto fixed container paths, which works identically on all
    three operating systems.
    """
    src = Path(input_path).resolve()
    out = Path(output_dir).resolve()
    ext = Path(external_dir).resolve() if external_dir else None

    out.mkdir(parents=True, exist_ok=True)

    mounts = [(src, C_IN), (out, C_OUT)]
    if ext:
        mounts.append((ext, C_EXT))

    args = [C_IN, C_OUT, input_type,
            "--work-dir", C_OUT, "--n-procs", str(n_procs),
            # The archive lands in a folder inside the output folder, which is
            # already bind-mounted, so it needs no mount of its own.
            "--zip-dir", f"{C_OUT}/{ZIP_SUBDIR}",
            # Every subject in one archive, rather than one per ten.
            "--subjects-per-tar", str(max(1, subjects))]
    if scheduler_budget_gb:
        # Otherwise the pipeline assumes 4 GB x n_procs, regardless of what
        # Docker actually has, and schedules straight past it.
        args += ["--mem-gb", str(scheduler_budget_gb)]
    if ext:
        args += ["--external-subjects-dir", C_EXT]
    args += pipeline_flags

    cmd = [runtime, "run", "--rm", "--name", CONTAINER_NAME] + platform_args()
    if memory_cap_gb:
        # The backstop: Docker kills the container instead of the host killing
        # Docker's virtual machine.
        cmd += ["--memory", f"{memory_cap_gb:.1f}g"]
    for host, dest in mounts:
        cmd += ["-v", f"{host}:{dest}"]
    for key, value in CONTAINER_ENV.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [IMAGE]

    return cmd + args
