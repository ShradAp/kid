"""A private Windows job closes the App Server and its MCP descendants."""
import ctypes as C
from ctypes import wintypes as W
import threading
import time


class Basic(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                ("min_set", C.c_size_t), ("max_set", C.c_size_t), ("active", W.DWORD),
                ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class Io(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class Limits(C.Structure):
    _fields_ = [("basic", Basic), ("io", Io), ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class ProcessJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        signatures = {"CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL), "CloseHandle": ([W.HANDLE], W.BOOL),
            "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
            "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL)}
        for name, (arguments, result) in signatures.items():
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            self.close()
            raise C.WinError(C.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise C.WinError(C.get_last_error())

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None

    def terminate(self):
        with self.lock:
            if not self.handle:
                return
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise C.WinError(C.get_last_error())
            # Termination of descendants is asynchronous. Do not return a
            # completed/cancelled command while children still hold its cwd.
            deadline = time.monotonic() + 5
            accounting = (C.c_ubyte * 48)()
            while time.monotonic() < deadline:
                if not self.kernel.QueryInformationJobObject(self.handle, 1, accounting, 48, None):
                    raise C.WinError(C.get_last_error())
                if C.c_uint32.from_buffer(accounting, 40).value == 0:
                    return
                time.sleep(.02)
            raise RuntimeError("Дочерние процессы Windows ещё завершаются")
