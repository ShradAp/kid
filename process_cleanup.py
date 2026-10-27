"""Windows Job Object: дочерние процессы завершаются вместе с приложением."""
import os

_job_handle = None
_instance_handles = []


def desktop_instance(on_activate):
    """Повторный запуск поднимает существующее окно через локальное событие ОС.

    Имена привязаны к проекту; сеть, файлы с токенами и HTTP API не нужны.
    Дескрипторы удерживаются до выхода процесса, включая аварийное завершение.
    """
    if os.name != "nt":
        return True
    import ctypes
    import hashlib
    import threading
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    identity = hashlib.sha256(os.path.normcase(os.path.abspath(__file__)).encode()).hexdigest()[:24]
    name = "Local\\KidDesktop-" + identity
    event = kernel.CreateEventW(None, False, False, name + "-activate")
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())
    mutex = kernel.CreateMutexW(None, False, name + "-instance")
    error = ctypes.get_last_error()
    if not mutex:
        kernel.CloseHandle(event)
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        signaled = kernel.SetEvent(event)
        kernel.CloseHandle(mutex)
        kernel.CloseHandle(event)
        if not signaled:
            raise ctypes.WinError(ctypes.get_last_error())
        return False
    _instance_handles.extend((mutex, event))

    def listen():
        while kernel.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
            on_activate()

    threading.Thread(target=listen, daemon=True, name="kid-window-activation").start()
    return True


def retain_child_process_job() -> None:
    """Удерживать job до выхода Python, включая аварийный os._exit.

    Закрытие последнего handle уничтожает всё дерево, поэтому handle намеренно
    не закрывается вручную: Windows освобождает его при завершении Python.
    Чужие процессы (например, ранее запущенный сервер) в job не включаются.
    """
    global _job_handle
    if os.name != "nt" or _job_handle is not None:
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    _job_handle = handle
