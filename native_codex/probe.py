"""Run only AFTER WFP rule verification; never sends HTTP to OpenAI.

An independent, dedicated copy of Python is protected with identical rules.
Tests use controlled numeric loopback destinations. TCP needs WSAEACCES;
UDP needs a positive unprotected delivery control and a protected packet drop.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import socket
from contextlib import ExitStack

from .wfp import ProtectionUnavailable, verify


PROBE_CODE = r'''
import json, socket, sys
results=[]
for family,kind,host,port in json.loads(sys.argv[1]):
 with socket.socket(family,kind) as s:
  s.settimeout(2)
  try:
   if kind==socket.SOCK_STREAM: s.connect((host,port))
   else: s.sendto(b'kid-protected-probe',(host,port))
  except OSError as e:
   results.append({'family':family,'kind':kind,'denied':e.winerror==10013,'error':e.winerror})
  else: results.append({'family':family,'kind':kind,'denied':False,'sent':True})
print(json.dumps(results))
'''


def verify_transports(probe_executable, python_home, broker_port):
    verify(probe_executable, broker_port)  # Absolute gate BEFORE test process.
    environment = {key: value for key, value in os.environ.items() if not key.upper().endswith("PROXY")}
    environment["PYTHONHOME"] = str(Path(python_home).resolve(strict=True))
    # UDP sendto can return success even when ALE drops its packet. Use real
    # controlled receivers and a positive unprotected control for EVERY path.
    # Only loopback is used, so a broken policy cannot leak even probe traffic
    # onto the Internet. IPv6 ::1 works without an external IPv6 route.
    with ExitStack() as stack:
        receivers, destinations = [], []
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                receiver = stack.enter_context(socket.socket(family, kind))
                receiver.bind((host, 0))
                receiver.settimeout(.4)
                if kind == socket.SOCK_STREAM:
                    receiver.listen(4)
                destination = (host, receiver.getsockname()[1])
                with socket.socket(family, kind) as control:
                    control.settimeout(2)
                    if kind == socket.SOCK_STREAM:
                        control.connect(destination)
                        accepted, _ = receiver.accept()
                        accepted.close()
                    else:
                        control.sendto(b"positive-control", destination)
                        if receiver.recv(128) != b"positive-control":
                            raise ProtectionUnavailable("Контрольный локальный UDP недоступен")
                receivers.append(receiver)
                destinations.append((family, kind, *destination))
        result = subprocess.run([str(probe_executable), "-S", "-c", PROBE_CODE, json.dumps(destinations)],
                                env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            checks = json.loads(result.stdout) if result.returncode == 0 else []
            if len(checks) != 4:
                raise ValueError()
            for check, receiver, destination in zip(checks, receivers, destinations):
                if (check.get("family"), check.get("kind")) != destination[:2]:
                    raise ValueError()
                if destination[1] == socket.SOCK_STREAM:
                    if check.get("denied") is not True:
                        raise ValueError()
                elif check.get("denied") is not True:
                    if check.get("sent") is not True:
                        raise ValueError()
                    try:
                        receiver.recv(128)
                    except socket.timeout:
                        pass
                    else:
                        raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ProtectionUnavailable("Блокировка прямых IPv4/IPv6 TCP/UDP не подтверждена; Codex остановлен") from None
    return {"direct_transports_blocked": 4, "enforcement": "TCP WSAEACCES; UDP controlled receiver",
            "positive_controls_passed": 4, "external_probe_packets": 0, "model_requests": 0}
