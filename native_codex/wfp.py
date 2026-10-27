"""Small binding to Windows' built-in filtering platform; no driver/dependency.

Policies are persistent, not dynamic: closing/crashing Python cannot remove the
deny rules. Only install() mutates Windows; verify() and describe() are read-only.
The three rules for each exact executable allow TCP to one IPv4 loopback port
and block every other IPv4/IPv6 connection, irrespective of proxy environment.
Unrelated executables (including host tools) are outside this policy.
"""
from __future__ import annotations

import ctypes as C
from contextlib import contextmanager
import os
from pathlib import Path
import uuid


class ProtectionUnavailable(RuntimeError):
    pass


U8, U16, U32, U64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64


class Guid(C.Structure):
    _fields_ = [("a", U32), ("b", U16), ("c", U16), ("d", U8 * 8)]

    @classmethod
    def make(cls, value):
        return cls.from_buffer_copy(uuid.UUID(str(value)).bytes_le)


class Blob(C.Structure):
    _fields_ = [("size", U32), ("data", C.POINTER(U8))]


class Display(C.Structure):
    _fields_ = [("name", C.c_wchar_p), ("description", C.c_wchar_p)]


class ValueUnion(C.Union):
    _fields_ = [("u8", U8), ("u16", U16), ("u32", U32), ("pointer", C.c_void_p)]


class Value(C.Structure):
    _fields_ = [("type", U32), ("value", ValueUnion)]


class Condition(C.Structure):
    _fields_ = [("key", Guid), ("match", U32), ("value", Value)]


class Action(C.Structure):
    _fields_ = [("type", U32), ("key", Guid)]


class Context(C.Union):
    _fields_ = [("raw", U64), ("key", Guid)]


class Filter(C.Structure):
    _fields_ = [("key", Guid), ("display", Display), ("flags", U32),
                ("provider", C.POINTER(Guid)), ("data", Blob), ("layer", Guid),
                ("sublayer", Guid), ("weight", Value), ("count", U32),
                ("conditions", C.POINTER(Condition)), ("action", Action),
                ("context", Context), ("reserved", C.POINTER(Guid)),
                ("id", U64), ("effective_weight", Value)]


class Sublayer(C.Structure):
    _fields_ = [("key", Guid), ("display", Display), ("flags", U32),
                ("provider", C.POINTER(Guid)), ("data", Blob), ("weight", U16)]


# GUIDs and layouts from Windows SDK fwpmu.h, fwpmtypes.h and fwptypes.h.
V4 = "c38d57d1-05a7-4c33-904f-7fbceee60e82"
V6 = "4a72393b-319f-44bc-84c3-ba54dcb3b6b4"
APP = "d78e1e87-8644-4ea5-9437-d809ecefc971"
ADDRESS = "b235ae9a-1d64-49b8-a44c-5ff3d9095045"
PORT = "c35a604d-d22b-4e1a-91b4-68f674ee674b"
PROTOCOL = "3971ef2b-623e-4f9a-8cb1-6e79b806b9a7"
NAMESPACE = uuid.UUID("1a1a3fe0-c7e7-437e-8459-70f29b281d97")
SUBLAYER = str(uuid.uuid5(NAMESPACE, "kid-native-codex-v1"))
PERSISTENT, BLOCK, PERMIT = 1, 0x1001, 0x1002


def _path(executable):
    path = Path(executable)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise ProtectionUnavailable("Защищаемый файл должен находиться на локальном диске")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ProtectionUnavailable("Ссылки в пути защищаемого файла запрещены")
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".exe":
        raise ProtectionUnavailable("Защищаемый исполняемый файл не найден")
    return str(path)


def describe(executable, broker_port):
    """Reviewable rule specification. No network, UAC or filtering changes."""
    path = _path(executable)
    if type(broker_port) is not int or not 1024 <= broker_port <= 65535:
        raise ProtectionUnavailable("Некорректный локальный порт посредника")
    rules = []
    for name, layer, allow in (("loopback", V4, True), ("deny-v4", V4, False), ("deny-v6", V6, False)):
        conditions = [(APP, 12, path)]
        if allow:
            conditions += [(ADDRESS, 3, 0x7F000001), (PORT, 2, broker_port), (PROTOCOL, 1, 6)]
        rules.append({"key": str(uuid.uuid5(NAMESPACE, path.casefold() + ":" + name)),
                      "name": "kid Codex: " + name, "layer": layer, "action": PERMIT if allow else BLOCK,
                      "weight": 15 if allow else 1, "conditions": conditions})
    return {"executable": path, "persistent": True, "broker": f"127.0.0.1:{broker_port}", "rules": rules}


def _api():
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise ProtectionUnavailable("Защита требует 64-битные Windows и Python")
    dll = C.WinDLL("fwpuclnt.dll", use_last_error=True)
    signatures = {
        "FwpmEngineOpen0": ([C.c_wchar_p, U32, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)], U32),
        "FwpmEngineClose0": ([C.c_void_p], U32),
        "FwpmGetAppIdFromFileName0": ([C.c_wchar_p, C.POINTER(C.POINTER(Blob))], U32),
        "FwpmFreeMemory0": ([C.POINTER(C.c_void_p)], None),
        "FwpmSubLayerAdd0": ([C.c_void_p, C.POINTER(Sublayer), C.c_void_p], U32),
        "FwpmSubLayerGetByKey0": ([C.c_void_p, C.POINTER(Guid), C.POINTER(C.POINTER(Sublayer))], U32),
        "FwpmFilterAdd0": ([C.c_void_p, C.POINTER(Filter), C.c_void_p, C.POINTER(U64)], U32),
        "FwpmFilterGetByKey0": ([C.c_void_p, C.POINTER(Guid), C.POINTER(C.POINTER(Filter))], U32),
        "FwpmTransactionBegin0": ([C.c_void_p, U32], U32),
        "FwpmTransactionCommit0": ([C.c_void_p], U32),
        "FwpmTransactionAbort0": ([C.c_void_p], U32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
    return dll


def _check(code):
    if code:
        # Numeric errors only; no raw user/network inputs in diagnostics.
        raise ProtectionUnavailable(f"Защита Windows недоступна (WFP 0x{code:08x})")


@contextmanager
def _security_descriptor():
    # Ordinary kid must be able to read its exact installed rules, without
    # elevation, enumeration access to global policy, or permission to change
    # them. Administrators/SYSTEM retain control; authenticated users get only
    # READ_CONTROL | FWPM_ACTRL_READ on THESE objects (not on the engine).
    api = C.WinDLL("advapi32.dll", use_last_error=True)
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [C.c_wchar_p, U32, C.POINTER(C.c_void_p), C.POINTER(U32)]
    convert.restype = C.c_int
    kernel = C.WinDLL("kernel32.dll", use_last_error=True)
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [C.c_void_p], C.c_void_p
    pointer = C.c_void_p()
    if not convert("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00020080;;;AU)", 1, C.byref(pointer), None):
        _check(C.get_last_error())
    try:
        yield pointer
    finally:
        kernel.LocalFree(pointer)


@contextmanager
def engine():
    dll = _api()
    handle = C.c_void_p()
    _check(dll.FwpmEngineOpen0(None, 10, None, None, C.byref(handle)))
    try:
        yield dll, handle
    finally:
        dll.FwpmEngineClose0(handle)


def _free(dll, pointer):
    dll.FwpmFreeMemory0(C.cast(C.byref(pointer), C.POINTER(C.c_void_p)))


def _app_id(dll, path):
    pointer = C.POINTER(Blob)()
    _check(dll.FwpmGetAppIdFromFileName0(path, C.byref(pointer)))
    try:
        return C.string_at(pointer.contents.data, pointer.contents.size)
    finally:
        _free(dll, pointer)


def _make_filter(rule, app_id):
    """Keep pointed-to bytes alive through the synchronous API call."""
    app_buffer = (U8 * len(app_id)).from_buffer_copy(app_id)
    app_blob = Blob(len(app_id), app_buffer)
    conditions = (Condition * len(rule["conditions"]))()
    for index, (key, kind, value) in enumerate(rule["conditions"]):
        condition = conditions[index]
        condition.key, condition.match, condition.value.type = Guid.make(key), 0, kind
        if kind == 12:
            condition.value.value.pointer = C.cast(C.pointer(app_blob), C.c_void_p)
        else:
            setattr(condition.value.value, {1: "u8", 2: "u16", 3: "u32"}[kind], value)
    result = Filter()
    result.key = Guid.make(rule["key"])
    result.display = Display(rule["name"], "Only the authenticated kid loopback broker; no direct network")
    result.flags, result.layer, result.sublayer = PERSISTENT, Guid.make(rule["layer"]), Guid.make(SUBLAYER)
    result.weight.type, result.weight.value.u8 = 1, rule["weight"]
    result.count, result.conditions, result.action.type = len(conditions), conditions, rule["action"]
    return result, (app_buffer, app_blob, conditions)


def _equal_filter(actual, rule, app_id):
    # BFE may add INDEXED (0x40) during insertion. It changes lookup cost,
    # not enforcement; all other unexpected flags (especially DISABLED) fail.
    if (bytes(actual.key) != bytes(Guid.make(rule["key"])) or actual.flags not in (PERSISTENT, PERSISTENT | 0x40)
            or bytes(actual.layer) != bytes(Guid.make(rule["layer"]))
            or bytes(actual.sublayer) != bytes(Guid.make(SUBLAYER))
            or actual.action.type != rule["action"] or actual.weight.type != 1
            or actual.weight.value.u8 != rule["weight"] or actual.count != len(rule["conditions"])):
        return False
    seen = []
    for index in range(actual.count):
        item = actual.conditions[index]
        if item.match != 0:
            return False
        kind = item.value.type
        if kind == 12:
            if not item.value.value.pointer:
                return False
            blob = C.cast(item.value.value.pointer, C.POINTER(Blob)).contents
            if blob.size != len(app_id):
                return False
            value = C.string_at(blob.data, blob.size)
        elif kind in (1, 2, 3):
            value = getattr(item.value.value, {1: "u8", 2: "u16", 3: "u32"}[kind])
        else:
            return False
        seen.append((bytes(item.key), kind, value))
    expected = [(bytes(Guid.make(key)), kind, app_id if kind == 12 else value)
                for key, kind, value in rule["conditions"]]
    return sorted(seen) == sorted(expected)


def verify(executable, broker_port):
    specification = describe(executable, broker_port)
    with engine() as (dll, handle):
        sublayer = C.POINTER(Sublayer)()
        _check(dll.FwpmSubLayerGetByKey0(handle, C.byref(Guid.make(SUBLAYER)), C.byref(sublayer)))
        try:
            # BFE resolves occupied sublayer priorities to nearby free values.
            # Permit-vs-deny order is set by our per-filter weights within this
            # exact sublayer, which are checked below without relaxation.
            if sublayer.contents.flags != PERSISTENT or not 0x7000 <= sublayer.contents.weight <= 0x7FFF:
                raise ProtectionUnavailable("Политика защиты Windows изменена")
        finally:
            _free(dll, sublayer)
        app_id = _app_id(dll, specification["executable"])
        for rule in specification["rules"]:
            pointer = C.POINTER(Filter)()
            _check(dll.FwpmFilterGetByKey0(handle, C.byref(Guid.make(rule["key"])), C.byref(pointer)))
            try:
                if not _equal_filter(pointer.contents, rule, app_id):
                    raise ProtectionUnavailable("Правила защиты Windows изменены; запуск Codex заблокирован")
            finally:
                _free(dll, pointer)
    return {"verified": True, "persistent": True, "ipv4": "blocked", "ipv6": "blocked",
            "allowed": specification["broker"]}


def diagnose(executable, broker_port):
    specification = describe(executable, broker_port)
    result = []
    with engine() as (dll, handle):
        app_id = _app_id(dll, specification["executable"])
        for rule in specification["rules"]:
            pointer = C.POINTER(Filter)()
            code = dll.FwpmFilterGetByKey0(handle, C.byref(Guid.make(rule["key"])), C.byref(pointer))
            if code:
                result.append({"name": rule["name"], "error": code})
                continue
            try:
                item = pointer.contents
                conditions = []
                for index in range(item.count):
                    condition = item.conditions[index]
                    kind = condition.value.type
                    if kind == 12:
                        blob = C.cast(condition.value.value.pointer, C.POINTER(Blob)).contents
                        value = C.string_at(blob.data, blob.size) == app_id
                    else:
                        value = condition.value.value.u32
                    conditions.append({"key": str(uuid.UUID(bytes_le=bytes(condition.key))),
                                       "match": condition.match, "type": kind, "value": value})
                result.append({"name": rule["name"], "flags": item.flags, "action": item.action.type,
                               "weight_type": item.weight.type, "weight": item.weight.value.u8,
                               "conditions": conditions, "matches": _equal_filter(item, rule, app_id)})
            finally:
                _free(dll, pointer)
    return result


def install(executables, broker_port):
    """Explicitly invoked elevated setup ONLY; never called during app startup.

Add all rules atomically. Existing unexpected rules cause a rollback, never
delete or weaken an existing block to repair a failed setup.
"""
    specifications = [describe(path, broker_port) for path in executables]
    if not specifications:
        raise ProtectionUnavailable("Не указаны защищаемые файлы")
    if not C.windll.shell32.IsUserAnAdmin():
        raise ProtectionUnavailable("Для однократной установки защиты требуется подтверждение Windows")
    with engine() as (dll, handle), _security_descriptor() as descriptor:
        _check(dll.FwpmTransactionBegin0(handle, 0))
        try:
            sublayer = Sublayer()
            sublayer.key, sublayer.display = Guid.make(SUBLAYER), Display("kid Codex protection", "Persistent fail-closed policy")
            sublayer.flags, sublayer.weight = PERSISTENT, 0x7FFF
            code = dll.FwpmSubLayerAdd0(handle, C.byref(sublayer), descriptor)
            if code != 0x80320009:  # FWP_E_ALREADY_EXISTS
                _check(code)
            for specification in specifications:
                app_id = _app_id(dll, specification["executable"])
                for rule in specification["rules"]:
                    current = C.POINTER(Filter)()
                    code = dll.FwpmFilterGetByKey0(handle, C.byref(Guid.make(rule["key"])), C.byref(current))
                    if code == 0:
                        try:
                            if not _equal_filter(current.contents, rule, app_id):
                                raise ProtectionUnavailable("Существующая политика отличается; автоматическая замена запрещена")
                        finally:
                            _free(dll, current)
                    elif code == 0x80320003:  # FWP_E_FILTER_NOT_FOUND
                        item, keepalive = _make_filter(rule, app_id)
                        _check(dll.FwpmFilterAdd0(handle, C.byref(item), descriptor, None))
                    else:
                        _check(code)
            _check(dll.FwpmTransactionCommit0(handle))
        except BaseException:
            dll.FwpmTransactionAbort0(handle)
            raise
    return [verify(item["executable"], broker_port) for item in specifications]
