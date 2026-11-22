"""Bounded loopback transport; never redirects to another endpoint."""
import json
import os
from pathlib import Path
import http.client
import socket
import time


class ObservationError(RuntimeError):
    pass


class DeadlineSocket(socket.socket):
    """SocketIO (used by HTTPResponse headers/body) calls this recv_into.

    Each I/O gets only the remaining total request budget. No timers or worker
    threads survive a call, and slow progress cannot renew the deadline.
    """
    def __init__(self, deadline):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.deadline = deadline

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('DevBench total request deadline exceeded')
        self.settimeout(remaining)

    def connect(self, address):
        self.remaining()
        return super().connect(address)

    def sendall(self, data, flags=0):
        self.remaining()
        return super().sendall(data, flags)

    def recv_into(self, buffer, nbytes=0, flags=0):
        self.remaining()
        return super().recv_into(buffer, nbytes, flags)


def process_executable(pid):
    """Read OS process identity without launching a helper or touching the game."""
    if os.name != 'nt':
        return Path('/proc').joinpath(str(pid), 'exe').resolve(strict=True)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError('Cannot query DevBench process executable')
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise OSError('Cannot query DevBench process executable')
        return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)


class Client:
    def __init__(self, game_dir, timeout):
        self.root = Path(game_dir).resolve()
        runtime = self.root / 'Data/SKSE/Plugins/devbench/runtime.json'
        self.expected = json.loads(runtime.read_text(encoding='utf-8-sig')) if runtime.exists() else {}
        try:
            self.port = int(os.environ.get('DEVBENCH_PORT') or self.expected.get('port') or (8921 if (self.root / 'SkyrimVR.exe').exists() else 8920))
        except (TypeError, ValueError) as exc:
            raise ObservationError('Invalid DevBench port') from exc
        if not 1 <= self.port <= 65535:
            raise ObservationError('Invalid DevBench port')
        self.timeout = timeout
        self.identity = self.request('/api/health')
        for key in ('pid', 'exe'):
            if key in self.expected and self.identity.get(key) != self.expected[key]:
                raise ObservationError('Wrong DevBench instance: ' + key)
        if self.identity.get('port', self.port) != self.port:
            raise ObservationError('Wrong DevBench instance: port')
        vr = (self.root / 'SkyrimVR.exe').exists()
        if self.identity.get('vr') is not vr:
            raise ObservationError('Wrong DevBench instance: runtime')
        if not isinstance(self.identity.get('pid'), int) or not self.identity.get('exe'):
            raise ObservationError('DevBench instance identity unavailable')
        if not (self.expected.get('pid') and self.expected.get('exe')):
            try:
                reported = Path(self.identity['exe'])
                # Linux fixture servers can publish an absolute executable identity;
                # Windows basename health requires the PID's actual OS image path.
                executable = reported.resolve() if os.name != 'nt' and reported.is_absolute() else process_executable(self.identity['pid'])
                expected_exe = self.root / ('SkyrimVR.exe' if vr else 'SkyrimSE.exe')
                if executable != expected_exe.resolve():
                    raise ObservationError('Wrong DevBench instance: executable belongs to another game root')
            except OSError as exc:
                raise ObservationError('DevBench instance executable could not be verified: ' + str(exc)) from exc

    def request(self, path, body=None):
        raw = None if body is None else json.dumps(body).encode()
        deadline = time.monotonic() + self.timeout
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        owned_socket = DeadlineSocket(deadline)
        try:
            owned_socket.connect(('127.0.0.1', self.port))
            connection.sock = owned_socket
            connection.request('GET' if body is None else 'POST', path, body=raw,
                               headers={'Content-Type': 'application/json'})
            with connection.getresponse() as response:
                if not 200 <= response.status < 300:
                    raise ObservationError('DevBench HTTP error: ' + str(response.status))
                data = response.read(4 * 1024 * 1024 + 1)
            if time.monotonic() > deadline:
                raise ObservationError('DevBench total request deadline exceeded')
            if len(data) > 4 * 1024 * 1024:
                raise ObservationError('DevBench response exceeded bound')
            result = json.loads(data)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ObservationError('DevBench request failed: ' + str(exc)) from exc
        finally:
            connection.close()
            owned_socket.close()
        if not isinstance(result, dict):
            raise ObservationError('DevBench response is not an object')
        if result.get('error') or result.get('ok') is False:
            raise ObservationError('DevBench error: ' + str(result.get('error', result)))
        return result

    def tool(self, name, args):
        return self.request('/api/tool/' + name, args)

    def tools(self):
        values = self.request('/api/tools').get('tools', [])
        return {x if isinstance(x, str) else x.get('name') for x in values}
