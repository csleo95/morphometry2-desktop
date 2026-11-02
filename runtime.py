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

import csv
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# Where a site sends its results, and its errors when a run does not finish.
CONTACT_EMAILS = [
    "leonardo.saraiva@yale.edu",
    "iss31@cam.ac.uk",
]

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


# Below this much RAM the computer is treated as small: the system is left
# less, so that the pipeline can at least be attempted.
SMALL_HOST_GB = 16.0


def host_reserve_gb(host_gb: float) -> float:
    """What the operating system and desktop keep for themselves.

    40% of the RAM, and never less than 6 GB - except on small computers,
    where 6 GB would leave nothing for the run, so 3 GB is kept instead.
    Measured here: a 27 GB VM on a 39 GB machine was killed by the kernel;
    with the desktop and browser holding 12-13 GB, 60% is what holds.
    """
    floor = 6.0 if host_gb >= SMALL_HOST_GB else 3.0
    return max(host_gb * 0.40, floor)


def desktop_reserve_gb(host_gb: float | None) -> float:
    """Left free for the desktop so it stays responsive while Docker
    restarts. Small computers cannot spare 4 GB for that."""
    if host_gb and host_gb < SMALL_HOST_GB:
        return 2.0
    return 4.0


def host_safe_gb(host_gb: float) -> float:
    """Most Docker may be configured for without putting the computer at risk.

    Deliberately based on TOTAL memory, not on what is free at this instant.
    Docker's limit is a persistent setting that has to hold when the virtual
    machine is full, and a figure derived from a momentarily-idle machine is
    one the computer cannot honour later. Never refuses: on a small computer
    the answer is small, but the run is still attempted with it.
    """
    return max(4.0, host_gb - host_reserve_gb(host_gb))


def can_start_with_gb(gb: float) -> bool:
    """Would Docker actually start if asked for this much right now?

    Separate from host_safe_gb: that is about whether a setting is sane at
    all, this is about whether the memory is there at this moment. Restarting
    Docker releases whatever its virtual machine currently holds, so that
    counts as available too.
    """
    free = host_free_gb()
    if free is None:
        return True                               # cannot tell; do not block
    setting = read_memory_setting()
    releases = (setting[2] / 1024) if setting and setting[2] else 0.0
    return (free + releases) >= (gb + desktop_reserve_gb(host_memory_gb()))


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


def _augment_path() -> None:
    """An app opened from Finder gets only /usr/bin:/bin:/usr/sbin:/sbin, and
    Docker Desktop keeps its command-line tools elsewhere - so shutil.which
    alone would report Docker missing on a Mac where it is running."""
    if not IS_MAC:
        return
    extra = ["/usr/local/bin", str(Path.home() / ".docker/bin"),
             "/Applications/Docker.app/Contents/Resources/bin",
             "/opt/homebrew/bin"]
    have = os.environ.get("PATH", "").split(os.pathsep)
    add = [d for d in extra if os.path.isdir(d) and d not in have]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + have)


def detect_runtime() -> str | None:
    _augment_path()
    for name in ("docker", "podman"):     # identical CLI; podman is a drop-in
        if shutil.which(name):
            return name
    return None


def docker_flavour(runtime: str = "docker") -> str:
    """'desktop' or 'engine' - decided without the daemon, because the answer
    matters most when it is down. Docker Desktop for Linux is told apart from
    plain Docker Engine by its context name and its install folder; on macOS
    and Windows only Desktop exists in practice."""
    if IS_MAC or IS_WINDOWS:
        return "desktop"
    try:
        out = subprocess.run([runtime, "context", "show"], capture_output=True,
                             text=True, timeout=10, **no_console())
        name = out.stdout.strip().lower()
        if "desktop" in name:
            return "desktop"
        if name and name != "default":
            return "engine"
    except Exception:                             # noqa: BLE001
        pass
    if Path("/opt/docker-desktop").exists():
        return "desktop"
    return "engine"


def start_hint(flavour: str) -> str:
    """How to start Docker, for the kind that is actually installed."""
    if flavour == "engine":
        return "Start it with:  sudo systemctl start docker"
    if IS_LINUX:
        return ("Start Docker Desktop from the applications menu and wait for "
                "it to finish starting.")
    return "Start Docker Desktop and wait for the whale icon to stop animating."


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
    """Files matching the BIDS pattern, ignoring dotfiles as pybids does -
    macOS leaves "._name" companions beside real files on some volumes."""
    hits: list[Path] = []
    for ext in NIFTI_SUFFIXES:
        hits += [p for p in folder.rglob(f"{pattern}{ext}")
                 if not p.name.startswith(".")]
    return hits


def _apple_double(root: Path) -> int:
    """Hidden "._*" companion files under root. They are not scans."""
    try:
        return sum(1 for p in root.rglob("._*") if p.is_file())
    except OSError:
        return 0


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
    units: int = 0                 # what the pipeline iterates: sessions (bids) or files (flat)
    apple_double: int = 0          # hidden macOS "._*" files found
    both_t2_flair: list[str] = field(default_factory=list)
    problem: str | None = None
    blocking: str | None = None    # reported in the panel, and Run must not proceed
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
                n_t2 = len(_niftis(unit, "*_T2w"))
                n_flair = len(_niftis(unit, "*_FLAIR"))
                r.t2 += n_t2
                r.flair += n_flair
                if n_t1 and n_t2 and n_flair:
                    # dataset.py refuses such a session outright: only one of
                    # the two may refine the pial surface
                    r.both_t2_flair.append(
                        sub.name if unit is sub else f"{sub.name}/{unit.name}")

            if has_t1_here:
                r.subjects += 1
                sessions_per_subject.append(len(ses_dirs))
            else:
                r.skipped += 1

        r.longitudinal = any(n > 1 for n in sessions_per_subject)
        r.multi_run = any(n > 1 for n in t1_per_session)
        r.units = r.sessions          # one row per session, as dataset.py does
        r.apple_double = _apple_double(root)

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
        if r.longitudinal:
            r.lines.append(
                f"  to process      : {r.units} subject-sessions (one per "
                f"session; all go into one archive)")
        if r.apple_double:
            r.lines.append(
                f"  WARNING         : {r.apple_double} hidden macOS '._*' file(s) "
                f"found. They are ignored here, but delete them before sharing "
                f"the folder (on a Mac: dot_clean <folder>).")
        if r.both_t2_flair:
            shown = ", ".join(r.both_t2_flair[:5])
            more = (f" and {len(r.both_t2_flair) - 5} more"
                    if len(r.both_t2_flair) > 5 else "")
            r.blocking = (
                f"{len(r.both_t2_flair)} session(s) have both a T2w and a FLAIR "
                f"image ({shown}{more}). The pipeline accepts only one of the "
                f"two per session and refuses to start. Move the FLAIR or the "
                f"T2w files out of those sessions, then choose the folder again.")
            r.lines.append("  PROBLEM         : " + r.blocking)
        return r

    # -------------------------------------------------- flat
    if top_niftis:
        r.layout = "flat"
        r.t1 = len(top_niftis)
        r.subjects = len(top_niftis)      # flat: one file == one subject
        r.sessions = r.subjects
        r.units = r.subjects
        hidden = [p for p in top_niftis if p.name.startswith("._")]
        r.apple_double = len(hidden)
        r.lines = [
            "Detected layout: flat",
            f"  NIfTI files     : {r.t1}",
            f"  subjects        : {r.subjects}   (flat treats each file as one subject)",
            "  T2w / FLAIR     : not used in the flat layout",
        ]
        if hidden:
            # Unlike the BIDS reader, the flat reader takes every .nii file it
            # sees, so each of these would become a "subject" and fail.
            r.blocking = (
                f"{len(hidden)} hidden macOS '._*' file(s) are in this folder "
                f"(for example {hidden[0].name}). In the flat layout the "
                f"pipeline would treat each one as a scan and fail. Delete them "
                f"(on a Mac: dot_clean <folder>), then choose the folder again.")
            r.lines.append("  PROBLEM         : " + r.blocking)
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
    flavour: str = "desktop"        # or "engine"; decides how "start it" reads
    wsl: bool = False               # Windows: engine runs inside WSL 2
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

    r.flavour = docker_flavour(runtime)

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
            fix = start_hint(r.flavour)
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
        kernel = str(info.get("KernelVersion") or "").lower()
        r.wsl = IS_WINDOWS and ("wsl" in kernel or "microsoft" in kernel)
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
        blob = (out.stderr or "").lower()

        # ---- installed, but this user may not talk to the daemon. On Linux
        # this too arrives as exit 0 with empty server fields, so it has to
        # be caught here as well as on a non-zero exit.
        if "permission denied" in blob:
            r.errors.append(
                f"{runtime} is installed but this user is not allowed to use it.\n\n"
                "  sudo usermod -aG docker $USER\n\n"
                "You must log out and back in for this to take effect "
                "(a reboot also works), then reopen this app."
            )
        elif "unable to start" in hint.lower() or "qemu" in blob:
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
            fix = start_hint(r.flavour)
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
                f"or newer.\n\nPlease update "
                + ("Docker Desktop" if r.flavour == "desktop" else "Docker")
                + ", then close and reopen this app."
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


# What Docker's virtual machine keeps for itself, on top of the container.
VM_OVERHEAD_GB = 2.0


def run_plan(n_procs: int, host_gb: float | None) -> tuple[float, float]:
    """(Docker setting to ask for, container cap) for this many subjects.

    The ceiling applies to Docker's setting - the size of its virtual
    machine - so the container's share is that minus the VM's own needs. One
    place for this arithmetic, so what the RAM text shows is what Run does.
    """
    want = estimate_gb(n_procs)
    max_setting = (host_safe_gb(host_gb) if host_gb
                   else want + VM_OVERHEAD_GB)
    setting = min(want + VM_OVERHEAD_GB, max_setting)
    return setting, max(1.0, setting - VM_OVERHEAD_GB)


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



# ---------------------------------------------------------------------------
#  Docker Desktop's memory setting
#
#  Docker Desktop keeps its resource limits in a user-owned JSON file - no
#  sudo, no admin rights needed. So rather than asking anyone to go and move a
#  slider, the app sets what the run needs and puts it back afterwards.
# ---------------------------------------------------------------------------

def _settings_file() -> Path | None:
    if IS_MAC:
        base = Path.home() / "Library/Group Containers/group.com.docker"
    elif IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "Docker"
    else:
        base = Path.home() / ".docker/desktop"
    # settings-store.json is the current name; settings.json is the older one
    for name in ("settings-store.json", "settings.json"):
        if (base / name).is_file():
            return base / name
    return None


def _memory_key(path: Path) -> str:
    """The key Docker Desktop uses in this file: settings-store.json (current)
    has MemoryMiB, the older settings.json had memoryMiB."""
    return "MemoryMiB" if path.name == "settings-store.json" else "memoryMiB"


def read_memory_setting() -> tuple[Path, str, int | None] | None:
    """(file, key, MiB) Docker Desktop is configured for. MiB is None when
    the file exists but has no memory key yet - a fresh install, where Docker
    uses its own default until the slider is first moved."""
    path = _settings_file()
    if not path:
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:                             # noqa: BLE001
        return None
    for key in ("MemoryMiB", "memoryMiB"):
        if isinstance(data.get(key), int):
            return path, key, data[key]
    return path, _memory_key(path), None


def write_memory_setting(gb: float) -> bool:
    """Set Docker Desktop's memory limit. Applies on its next restart.

    Adds the key when the file has none - which is exactly what Docker
    Desktop itself does the first time the slider is moved, so the file stays
    one it reads back."""
    found = read_memory_setting()
    if not found:
        return False
    path, key, _ = found
    try:
        data = json.loads(path.read_text())
        data[key] = int(round(gb * 1024))
        path.write_text(json.dumps(data, indent=2))
        return True
    except Exception:                             # noqa: BLE001
        return False


def _engine_up(seconds: int) -> bool:
    """Poll until Docker's engine actually answers, or give up."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=20, **no_console())
            if out.returncode == 0 and out.stdout.strip():
                return True
        except Exception:                         # noqa: BLE001
            pass
        time.sleep(5)
    return False


def restart_desktop(timeout: int = 300) -> bool:
    """Restart Docker Desktop and wait for its engine to answer.

    Two ways, because the obvious one is not reliable: when Docker Desktop has
    been quit entirely, "docker desktop start" answers "Docker Desktop is
    already running" and does nothing, leaving the engine dead. On Linux the
    systemd user service does start it properly, so that is the fallback.

    Success is always decided by asking the engine, never by what a command
    printed.
    """
    for argv in (["docker", "desktop", "restart"],
                 ["systemctl", "--user", "restart", "docker-desktop"],
                 ["systemctl", "--user", "start", "docker-desktop"]):
        if argv[0] == "systemctl" and not IS_LINUX:
            continue
        try:
            subprocess.run(argv, capture_output=True, timeout=180,
                           **no_console())
        except Exception:                         # noqa: BLE001
            continue
        if _engine_up(timeout // 2):
            return True
    return False


def ensure_running(runtime: str) -> bool:
    """Make sure Docker is up, starting it if it is not."""
    if daemon_alive(runtime):
        return True
    return restart_desktop()


def ensure_memory(target_gb: float, wsl: bool | None = None) -> tuple[bool, str]:
    """Try to give Docker at least `target_gb`, and never make things worse.

    Raising the limit means restarting Docker, and a restart can fail - most
    often because the new figure is more than the computer can spare right
    then. If that happens the old value is put back and Docker is started
    again, so the machine is left working. The caller carries on with whatever
    memory Docker actually has; a failed raise is never a reason to abandon
    a run.
    """
    if wsl is None:
        wsl = wsl_backend()
    if wsl:
        # Docker Desktop on Windows normally runs inside WSL 2, and then its
        # memory is whatever WSL allows: the settings file is not consulted.
        return False, (
            f"On this computer Docker's memory is set by WSL 2, not by Docker "
            f"Desktop, so the app cannot raise it. To allow {target_gb:.0f} GB, "
            f"create or edit the file %UserProfile%\\.wslconfig with:\n"
            f"  [wsl2]\n  memory={target_gb:.0f}GB\n"
            f"then run  wsl --shutdown  and start Docker Desktop again."
        )

    found = read_memory_setting()
    if not found:
        return False, ("Docker's memory setting could not be found, so it was "
                       "left alone.")

    _, _, mib = found
    current = (mib / 1024) if mib else _docker_memory_gb()
    if current and current >= target_gb:
        return False, f"Docker already has {current:.0f} GB; nothing to change."

    if not write_memory_setting(target_gb):
        return False, ("Docker's memory setting could not be written, so it "
                       "was left alone.")

    if restart_desktop():
        # Report what Docker actually came back with, not what was asked for.
        actual = _docker_memory_gb()
        if actual is None:
            return True, f"Docker restarted; it was asked for {target_gb:.0f} GB."
        if actual + 1.0 < target_gb:
            return False, (f"Docker restarted with {actual:.0f} GB, not the "
                           f"{target_gb:.0f} GB asked for. The run will use "
                           f"what it has.")
        return True, f"Docker now has {actual:.0f} GB."

    # It did not come back. Undo, and get Docker running again either way.
    if mib:
        write_memory_setting(current)
    back = restart_desktop()
    shown = f"{current:.0f} GB" if current else "its previous value"
    return False, (
        f"Docker could not start with {target_gb:.0f} GB, so its setting was "
        f"put back to {shown}"
        + (" and it is running again." if back else
           " - but it did not restart. Start Docker Desktop, then press Run again.")
    )


def _docker_memory_gb() -> float | None:
    """What the engine reports it has right now."""
    try:
        out = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"],
                             capture_output=True, text=True, timeout=20,
                             **no_console())
        value = int(out.stdout.strip())
        return round(value / 1024**3, 1) if value else None
    except Exception:                             # noqa: BLE001
        return None


def wsl_backend() -> bool:
    """Is Docker Desktop on Windows running on WSL 2? When the engine is down
    this cannot be read, and WSL 2 is the default, so assume yes."""
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.run(["docker", "info", "--format", "{{.KernelVersion}}"],
                             capture_output=True, text=True, timeout=20,
                             **no_console())
        kernel = out.stdout.strip().lower()
        if kernel:
            return "wsl" in kernel or "microsoft" in kernel
    except Exception:                             # noqa: BLE001
        pass
    return True


def daemon_alive(runtime: str) -> bool:
    """Is Docker still there? After a crash it usually is not."""
    try:
        out = subprocess.run([runtime, "info", "--format", "{{.ServerVersion}}"],
                             capture_output=True, text=True, timeout=15,
                             **no_console())
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:                             # noqa: BLE001
        return False


def container_running(runtime: str) -> bool:
    """Is a run already going? Prevents a second one clobbering the output."""
    try:
        out = subprocess.run(
            [runtime, "ps", "--filter", f"name=^{CONTAINER_NAME}$",
             "--filter", "status=running", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=20, **no_console())
        return CONTAINER_NAME in out.stdout
    except Exception:                             # noqa: BLE001
        return False


def stop_container(runtime: str, grace: int = 10) -> None:
    """Ask the daemon to stop the run. Safe to call when nothing is running.

    `grace` is how long the container gets to exit on SIGTERM before it is
    killed. A part-done subject is discarded and redone on the next run either
    way, so there is little to gain from waiting long - and on window close a
    long wait just looks like the app has hung.
    """
    try:
        subprocess.run([runtime, "stop", "-t", str(grace), CONTAINER_NAME],
                       capture_output=True, timeout=grace + 10, **no_console())
    except Exception:                             # noqa: BLE001
        pass


def diagnose_failure(runtime: str, code: int, tail: str,
                     output_dir: Path | None = None,
                     started_at: float | None = None,
                     step: str | None = None,
                     flavour: str = "desktop") -> str:
    """Turn an exit code into something the person can act on.

    The raw failure is almost never self-explanatory: a host out-of-memory
    kill surfaces only as "unexpected EOF" and exit 125, which says nothing
    about memory at all.
    """
    blob = (tail or "").lower()
    steps: list[str] = []
    host_gb = host_memory_gb()

    # Order matters: a specific signature is stronger evidence than "Docker
    # is not answering now", which may be true for an unrelated reason by the
    # time this runs.
    daemon_died = ("unexpected eof" in blob
                   or "cannot connect to the docker daemon" in blob
                   or "error during connect" in blob)
    killed = ("code -9" in blob or "signal 9" in blob
              or "signal: killed" in blob or "killed process" in blob)
    mounts_denied = ("mounts denied" in blob or "not shared from the host" in blob
                     or "is not shared" in blob)
    pulling = step == "Downloading the container"
    start_again = ("Start Docker Desktop again." if flavour == "desktop"
                   else "Start Docker again:  sudo systemctl start docker")

    # ---- a folder Docker Desktop is not allowed to see
    #
    # Also exit 125 and also from the daemon, so without a branch of its own
    # this read as Docker having died of memory.
    if mounts_denied:
        body = ("Docker is not allowed to see one of the folders you chose. "
                "Docker Desktop only shares certain folders with its virtual "
                "machine, and the scans or output folder is outside them.")
        steps.append("Open Docker Desktop > Settings > Resources > File Sharing "
                     "and add the folder (or a folder above it).")
        steps.append("Apply, wait for Docker to restart, then press Run again.")

    # ---- the image could not be downloaded
    elif pulling:
        body = "The container image could not be downloaded."
        if "no space left" in blob:
            body += (" The disk is full: the download is about 5.5 GB and the "
                     "image needs about 14 GB once unpacked.")
            steps.append("Free at least 20 GB, then press Run again.")
        elif "toomanyrequests" in blob or "rate limit" in blob:
            body += " Docker Hub is limiting downloads from this network right now."
            steps.append("Wait an hour and press Run again, or sign in to "
                         "Docker Desktop with a Docker Hub account.")
        elif ("no such host" in blob or "dial tcp" in blob or "timeout" in blob
              or "proxy" in blob or "tls" in blob):
            body += " This computer could not reach Docker Hub."
            steps.append("Check the internet connection. On an institutional "
                         "network a proxy may be needed: Docker Desktop > "
                         "Settings > Resources > Proxies.")
            steps.append("Then press Run again.")
        else:
            steps.append("Check the internet connection and that the disk has "
                         "about 20 GB free, then press Run again.")

    # ---- a step inside the run was killed for memory
    #
    # This is not exit 137: the container survives, one process inside it is
    # killed, and the pipeline then exits 1 like any other error. It shows up
    # as "subprocess exited with code -9" - signal 9, SIGKILL - which is what
    # the kernel sends when memory runs out. Without this branch it reads as
    # an ordinary pipeline error and sends people hunting through logs.
    elif code == 137 or killed:
        body = ("A step of the pipeline was stopped because it ran out of "
                "memory. Some steps use more memory than they declare, so "
                "this can happen even when the totals look like they fit.")
        steps.append("Lower \"Subjects in parallel\". Fewer at once leaves "
                     "more memory for each step.")
        steps.append("Close other programs, especially the web browser, "
                     "which frees memory the app can then give to Docker.")

    elif code == 127:
        body = f"{runtime} could not be started."
        steps.append(f"Check that {runtime} is installed and running.")

    # ---- Docker disappeared mid-run
    elif daemon_died or code == 125 or not daemon_alive(runtime):
        body = "Docker stopped responding while the pipeline was running."
        if host_gb:
            body += (f" This almost always means the computer ran out of "
                     f"memory and the system shut Docker down. This computer "
                     f"has {host_gb:.0f} GB in total.")
        steps.append(start_again)
        steps.append("Lower subjects in parallel, which lowers how much "
                     "memory the pipeline asks for.")

    # ---- the pipeline itself errored
    else:
        body = (f"The pipeline stopped with an error (exit {code}). The lines "
                f"above are its last output.")
        steps.append("Check the messages above for the first error.")
        steps.append("Details are written to the logs/errors folder inside "
                     "your output folder.")

    if not (pulling or mounts_denied):           # those already say so
        steps.append("Press Run again - subjects that already finished are "
                     "not redone, so it carries on from where it stopped.")

    numbered = "\n".join(f"  {n}. {s}" for n, s in enumerate(steps, 1))
    text = f"{body}\n\nWhat to do:\n{numbered}"

    # If it keeps failing, the site should not be left guessing who to tell -
    # nor be told to ship reports from attempts long since resolved.
    if output_dir is not None:
        out = Path(output_dir)
        errors_dir = Path(_where_errors(out))

        # The pipeline writes the archive before it raises, so a failed run
        # usually leaves a partial one behind. Say so, so it is not mistaken
        # for a finished result - or for something that needs deleting.
        if not pulling and sorted((out / ZIP_SUBDIR).glob("morphometry_zip_*.tar.xz")):
            text += (f"\n\nAn archive was still written to\n  {out / ZIP_SUBDIR}\n"
                     f"It is incomplete and is rewritten as more finishes; "
                     f"there is nothing to do with it yet.")

        if errors_dir.is_dir():
            this_run, earlier = crash_reports(out, started_at)
            if this_run:
                text += ("\n\nIf it fails again:\n"
                         + send_to_block(errors_dir,
                                         "these report(s) from this run, in this folder",
                                         this_run))
                if earlier:
                    text += (f"\n\n({len(earlier)} older report(s) from earlier "
                             f"attempts are also there; those need not be sent.)")
            else:
                text += ("\n\nIf it fails again:\n"
                         + send_to_block(errors_dir,
                                         "the contents of this folder"))
    return text


def _where_errors(out: Path) -> str:
    return str(out / "morphometry" / "logs" / "errors")


def send_to_block(out: Path, what: str, files: list[Path] | None = None) -> str:
    """The same closing instruction everywhere, so nobody has to hunt for it.

    `files` names the specific reports when only some of a folder's contents
    should go; they are listed under the folder, before the addresses.
    """
    text = f"Please send {what}:\n  {out}\n"
    if files:
        text += _name_list(files) + "\n"
    return text + "\nto:\n" + "\n".join(f"  {a}" for a in CONTACT_EMAILS)


_CRASH_STAMP = re.compile(r"crash-(\d{8})-(\d{6})")


def crash_reports(out: Path, started_at: float | None
                  ) -> tuple[list[Path], list[Path]]:
    """(this run's, earlier attempts') crash reports.

    The pipeline writes one file per failed step and never removes any, so
    after a few attempts the folder mixes live problems with ones long since
    retried and completed. Each name carries the moment it was written, e.g.
    crash-20260924-214350-..., by the container's clock, which is UTC. That is
    read in preference to the file's mtime because it survives the folder
    being copied. mtime is the fallback, and also the tie-breaker: if the two
    disagree by more than half an hour the name was not written in UTC after
    all, and mtime is the one that is an absolute instant.
    """
    folder = out / "morphometry" / "logs" / "errors"
    files = sorted(folder.glob("*.txt")) if folder.is_dir() else []
    if started_at is None:
        return files, []

    grace = 120.0                     # clock skew between Docker's VM and host
    this_run: list[Path] = []
    earlier: list[Path] = []
    for f in files:
        mtime = f.stat().st_mtime
        when = mtime
        hit = _CRASH_STAMP.search(f.name)
        if hit:
            try:
                named = datetime.strptime(hit.group(1) + hit.group(2),
                                          "%Y%m%d%H%M%S")
                named = named.replace(tzinfo=timezone.utc).timestamp()
                if abs(named - mtime) <= 1800:
                    when = named
            except ValueError:
                pass
        (this_run if when >= started_at - grace else earlier).append(f)
    return this_run, earlier


def tidy_resolved_crashes(out: Path) -> tuple[int, Path]:
    """Move every crash report aside once the archive is complete.

    Only called when nothing is missing. At that point every report is
    resolved by definition - the step that failed has since run to
    completion, or the archive would not be whole - so leaving them where
    "send this folder" points would ship non-problems. They are kept, not
    deleted, under errors/resolved/ in case anyone wants to look.
    """
    folder = out / "morphometry" / "logs" / "errors"
    resolved = folder / "resolved"
    moved = 0
    if folder.is_dir():
        for f in sorted(folder.glob("*.txt")):
            try:
                resolved.mkdir(exist_ok=True)
                f.rename(resolved / f.name)
                moved += 1
            except OSError:
                pass                              # leave it; the note says so
    return moved, resolved


def _name_list(files: list[Path], limit: int = 8) -> str:
    names = [f.name for f in files[:limit]]
    text = "\n".join(f"    {n}" for n in names)
    if len(files) > limit:
        text += f"\n    ... and {len(files) - limit} more"
    return text


def _dataset_ids(out: Path) -> set[str]:
    """The ids the pipeline gave the units it processes - sub-X, sub-X_ses-Y
    or a flat file's stem - from the dataset.csv it writes first thing."""
    csv_path = out / "morphometry" / "dataset.csv"
    try:
        with open(csv_path, newline="") as fh:
            return {row["subject_id"] for row in csv.DictReader(fh)
                    if row.get("subject_id")}
    except Exception:                             # noqa: BLE001
        return set()


def _units_in(missing_files: list[Path], out: Path) -> set[str]:
    """Which units the missing-file lists mention.

    Lines are paths relative to morphometry/, and the unit id sits at a
    different depth in different trees (recon-all/<id>/..., qc/mriqc/<id>/...,
    after2/<id>_retess/...), so the components are matched against the ids
    the pipeline itself recorded rather than parsed by position or by a
    sub-* pattern - which broke on longitudinal and flat datasets.
    """
    ids = _dataset_ids(out)
    found: set[str] = set()
    for m in missing_files:
        try:
            lines = m.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            for part in line.strip().split("/"):
                part = re.sub(r"_retess$", "", part)
                if ids:
                    if part in ids:
                        found.add(part)
                        break
                elif part.startswith("sub-"):     # no csv: best effort
                    found.add(part)
                    break
    return found


def summarise_outputs(output_dir: Path, subjects: int | None = None,
                      started_at: float | None = None,
                      unit_word: str = "subjects") -> str:
    """What the run produced: the archive to send, and whether anything failed.

    Read from disk rather than from the log, so it reflects what is actually
    there - including a previous run's archive when this run added nothing.
    """
    out = Path(output_dir)
    zip_dir = out / ZIP_SUBDIR
    zips = sorted(zip_dir.glob("morphometry_zip_*.tar.xz"))
    this_run, earlier = crash_reports(out, started_at)
    errors_dir = Path(_where_errors(out))

    # ---- 3. nothing to send
    #
    # Reached on a run that exited cleanly. The pipeline now writes an archive
    # on every run, complete or not, so arriving here means the archive step
    # itself did not run - rare, and the messages above will say why.
    if not zips:
        text = ("The pipeline finished, but no archive was written. It normally "
                "writes one even when subjects are incomplete, so something "
                "stopped it - check the messages above.\n\n"
                "Press Run again to carry on - finished work is not redone.")
        if this_run:
            text += (f"\n\n{len(this_run)} error report(s) were recorded "
                     f"during this run.\n\n"
                     + send_to_block(errors_dir,
                                     "these report(s) from this run, in this folder",
                                     this_run))
            if earlier:
                text += (f"\n\n({len(earlier)} older report(s) from earlier "
                         f"attempts are also there; those need not be sent.)")
        elif earlier:
            text += (f"\n\nNo error was recorded during this run. "
                     f"{len(earlier)} report(s) from earlier attempts remain in\n"
                     f"  {errors_dir}\n"
                     f"If it stops making progress, send those.")
        else:
            text += ("\n\nIf this repeats with nothing left to run:\n"
                     + send_to_block(errors_dir, "the contents of this folder"))
        return text

    total = sum(a.stat().st_size for a in zips) / 1024**3

    # Whether an archive is complete is decided by the file the pipeline
    # writes beside it, not by the crash folder. zip_outputs deletes
    # missing_files_<id>.txt on every pass and rewrites it only when something
    # is absent, so its presence is a reliable, self-clearing flag. Crash
    # reports on the other hand are never removed: one from a step that was
    # later retried and completed stays there for ever, and must not be read
    # as "the archive is incomplete".
    missing = [zip_dir / f"missing_files_{a.stem.split('_')[-1].split('.')[0]}.txt"
               for a in zips]
    missing = [m for m in missing if m.is_file()]

    # ---- 2. finished, but some subjects are still missing files
    if missing:
        affected = _units_in(missing, out)
        how_many = (f"{len(affected)} of {subjects} {unit_word}" if subjects
                    else f"{len(affected)} {unit_word}")
        text = (f"The pipeline finished, but {how_many} are not complete, "
                f"so the archive is incomplete.\n\n"
                f"The archive is in:\n  {zip_dir}   ({total:.1f} GB)\n\n")
        if this_run:
            text += (f"{len(this_run)} error report(s) were recorded during "
                     f"this run.\n\n"
                     + send_to_block(errors_dir,
                                     "these report(s) from this run, in this folder",
                                     this_run))
            if earlier:
                text += (f"\n\n({len(earlier)} older report(s) from earlier "
                         f"attempts are also there; those need not be sent.)")
        elif earlier:
            text += (f"No error was recorded during this run, so press Run "
                     f"again to carry on - finished work is not redone.\n\n"
                     f"({len(earlier)} report(s) from earlier attempts remain in "
                     f"{errors_dir}. If it stops making progress, send those.)")
        else:
            text += ("No error was recorded, so press Run again to carry on - "
                     "finished work is not redone.")
        return text

    # ---- 1. finished, every subject complete
    done = (f"all {subjects} {unit_word}" if subjects
            else f"every {unit_word.rstrip('s')}")
    text = (f"The pipeline finished with {done} complete.\n\n"
            + send_to_block(zip_dir, f"the archive in this folder ({total:.1f} GB)"))
    if this_run or earlier:
        # A crash report can only be left over from an earlier attempt: had
        # the failed step not been retried and completed, the archive would
        # not be complete now. Move them aside so the errors folder holds
        # only live problems from here on.
        moved, resolved = tidy_resolved_crashes(out)
        left = len(this_run) + len(earlier) - moved
        text += (f"\n\n({moved} crash report(s) from earlier attempts were "
                 f"moved to\n  {resolved}\nThose steps were retried and "
                 f"completed, so they need no action.")
        if left:
            text += (f" {left} could not be moved and remain in "
                     f"{errors_dir}; they need no action either.")
        text += ")"
    return text


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
