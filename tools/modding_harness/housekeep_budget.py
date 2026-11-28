"""Default resource bounds for dedicated Housekeep command processes."""
import ctypes as c
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as w
from pathlib import Path

from .locking import exclusive

CPU_RATE = 75  # Windows Job units are hundredths of total machine CPU.
MEMORY_LIMIT = 384 * 1024**2
BYTE_RATE = 4 * 1024**2
OPERATION_RATE = 400


@contextmanager
def worker_slot(root: Path):
    """OS-owned lock. A crash releases it without a stale-lock timeout."""
    path = root / ".claude" / "diag" / "housekeep" / "background.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(exclusive(path, wait=0))
        except TimeoutError:
            yield False
        else:
            yield True


class IoCounters(c.Structure):
    _fields_ = [(field, c.c_uint64) for field in
                ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]


class BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
        ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t),
        ("active_processes", w.DWORD), ("affinity", c.c_size_t),
        ("priority", w.DWORD), ("scheduling", w.DWORD),
    ]


class JobLimits(c.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io", IoCounters),
        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t),
    ]


class DiskPacer:
    """Pace both byte traffic and metadata, including calls inside zipfile and pathlib."""

    def __init__(self, read_counters, clock=time.monotonic, sleep=time.sleep):
        self.read_counters, self.clock, self.sleep = read_counters, clock, sleep
        self.previous = read_counters()
        self.byte_time = self.operation_time = clock()
        self.events = 0

    def checkpoint(self):
        byte_count, operations = self.read_counters()
        old_bytes, old_operations = self.previous
        self.previous = byte_count, operations
        now = self.clock()
        # At most 50 ms of idle credit. A long pause cannot fund a new burst.
        self.byte_time = max(self.byte_time, now - 0.05) + (byte_count - old_bytes) / BYTE_RATE
        self.operation_time = max(self.operation_time, now - 0.05) + (operations - old_operations) / OPERATION_RATE
        delay = max(self.byte_time, self.operation_time) - now
        if delay > 0:
            self.sleep(delay)

    def profile(self, frame, event, arg):
        self.events += 1
        if self.events == 32:
            self.events = 0
            self.checkpoint()


class PosixPacer(DiskPacer):
    """Pace CPU and count path operations that are absent from Linux I/O accounting."""

    path_calls = {"stat", "lstat", "scandir", "listdir", "readlink", "mkdir", "rmdir",
                  "rename", "replace", "unlink", "open", "access"}

    def __init__(self, proc_io):
        self.proc_io = proc_io
        self.path_operations = 0
        self.previous_cpu = time.process_time()
        self.cpu_time = time.monotonic()
        self.cpu_fraction = min(1, (os.cpu_count() or 1) * CPU_RATE / 10000)
        super().__init__(self.counters)

    def counters(self):
        if self.proc_io is None:
            return 0, self.path_operations
        self.proc_io.seek(0)
        record = dict(line.split() for line in self.proc_io)
        byte_count = int(record["rchar:"]) + int(record["wchar:"])
        ops = int(record["syscr:"]) + int(record["syscw:"])
        return byte_count, ops + self.path_operations

    def checkpoint(self):
        now = self.clock()
        cpu = time.process_time()
        self.cpu_time = max(self.cpu_time, now - 0.05) + (cpu - self.previous_cpu) / self.cpu_fraction
        self.previous_cpu = cpu
        if self.cpu_time > now:
            self.sleep(self.cpu_time - now)
        super().checkpoint()

    def profile(self, frame, event, arg):
        if event == "c_call" and getattr(arg, "__name__", "") in self.path_calls:
            self.path_operations += 1
        super().profile(frame, event, arg)


@contextmanager
def paced(pacer):
    previous = sys.getprofile()
    sys.setprofile(pacer.profile)
    try:
        yield
    finally:
        sys.setprofile(previous)
        pacer.checkpoint()


@contextmanager
def posix_budget():
    import resource

    os.nice(19)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    soft = MEMORY_LIMIT if soft == resource.RLIM_INFINITY else min(soft, MEMORY_LIMIT)
    resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
    with ExitStack() as stack:
        proc_io = stack.enter_context(open("/proc/self/io", encoding="ascii")) if sys.platform == "linux" else None
        stack.enter_context(paced(PosixPacer(proc_io)))
        yield


@contextmanager
def windows_budget():
    """Put this dedicated CLI worker and its children in a low-priority Windows Job."""
    kernel = c.WinDLL("kernel32", use_last_error=True)

    def api(name, result, *args):
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, args
        return function

    def check(value):
        if not value:
            raise c.WinError(c.get_last_error())
        return value

    current = api("GetCurrentProcess", w.HANDLE)()
    close = api("CloseHandle", w.BOOL, w.HANDLE)
    set_limits = api("SetInformationJobObject", w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    assign = api("AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
    priority = api("SetPriorityClass", w.BOOL, w.HANDLE, w.DWORD)
    get_io = api("GetProcessIoCounters", w.BOOL, w.HANDLE, c.POINTER(IoCounters))
    job = check(api("CreateJobObjectW", w.HANDLE, c.c_void_p, w.LPCWSTR)(None, None))
    try:
        limits = JobLimits()
        limits.basic.flags = 0x20 | 0x200  # idle priority class and job-wide committed memory
        limits.basic.priority = 0x40  # IDLE_PRIORITY_CLASS
        limits.job_memory = MEMORY_LIMIT
        check(set_limits(job, 9, c.byref(limits), c.sizeof(limits)))
        cpu = (w.DWORD * 2)(0x1 | 0x4, CPU_RATE)  # enabled, hard cap
        check(set_limits(job, 15, c.byref(cpu), c.sizeof(cpu)))
        check(assign(job, current))
        check(priority(current, 0x00100000))  # PROCESS_MODE_BACKGROUND_BEGIN
        io = IoCounters()

        def counters():
            check(get_io(current, c.byref(io)))
            return io.read_bytes + io.write_bytes, io.reads + io.writes + io.other

        with paced(DiskPacer(counters)):
            yield job
    finally:
        close(job)


def run(root, operation, *, busy_code=0):
    """Run one Housekeep CLI command under the default budget."""
    with ExitStack() as stack:
        try:
            acquired = stack.enter_context(worker_slot(root))
            if not acquired:
                if busy_code:
                    print("housekeep: another worker is active", file=sys.stderr)
                return busy_code
            budget = windows_budget if os.name == "nt" else posix_budget
            stack.enter_context(budget())
        except (OSError, ValueError) as exc:
            print(f"housekeep resource budget: {exc}", file=sys.stderr)
            return 1
        return operation()
