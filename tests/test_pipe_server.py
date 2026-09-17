"""Real Windows kernel-pipe tests; no QMT or market-data calls."""

import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from contextlib import contextmanager
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")


@pytest.fixture
def private_tmp_path():
    # The ordinary temp directory may permit other users to rename ancestors.
    directory = Path(tempfile.mkdtemp(prefix=".qmt-security-test-", dir=Path.home()))
    try:
        yield directory
    finally:
        resolved = directory.resolve()
        assert resolved.parent == Path.home().resolve()
        assert resolved.name.startswith(".qmt-security-test-")
        shutil.rmtree(resolved)


def _name():
    return r"\\.\pipe\qmt-bridge-test-" + uuid.uuid4().hex


def _server(name=None):
    from bigqmt_bridge.pipe_server import Server

    return Server(_name() if name is None else name)


def _client(name):
    import _winapi

    handle = _winapi.CreateFile(name, 0xC0000000, 0, 0, 3, 0x40000000, 0)
    _winapi.SetNamedPipeHandleState(handle, 2, None, None)
    return handle


def _until(action, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.001)
    pytest.fail("bounded pipe operation did not complete")


@pytest.mark.parametrize("name", ["", "plain", r"\\other\pipe\bridge", r"C:\pipe", "\\\\.\\pipe\\", "\\\\.\\pipe\\x\x00y"])
def test_server_rejects_nonlocal_or_invalid_names(name):
    with pytest.raises(ValueError):
        _server(name)


def test_accept_is_nonblocking_and_close_releases_unconnected_name():
    name = _name()
    server = _server(name)
    try:
        start = time.monotonic()
        for _ in range(100):
            assert server.accept_nonblocking() is None
        assert time.monotonic() - start < 1
    finally:
        server.close()
        server.close()
    replacement = _server(name)
    replacement.close()


def test_dacl_readback_reports_only_explicit_current_user_and_system():
    server = _server()
    try:
        report = server.security_report()
        assert report["dacl_verified"] is True
        assert report["dacl_protected"] is True
        assert report["allowed_principals"] == ["current_user", "SYSTEM"]
        assert report["remote_clients_rejected"] is True
        assert report["inheritable"] is False
        assert "S-1-" not in json.dumps(report)
        report["dacl_verified"] = False
        assert server.security_report()["dacl_verified"] is True
    finally:
        server.close()


def test_existing_name_cannot_be_joined_as_second_server():
    name = _name()
    first = _server(name)
    try:
        with pytest.raises(OSError) as error:
            _server(name)
        assert error.value.winerror in (5, 231)
    finally:
        first.close()


def test_actual_duplex_messages_and_handle_ownership_transfer():
    import _winapi

    name = _name()
    server = _server(name)
    client = None
    channel = None
    try:
        client = _client(name)
        channel = _until(server.accept_nonblocking)
        assert server.accept_nonblocking() is None
        server.close()  # accepted PipeIO owns the handle now
        messages = [b"one", b"two\x00with-boundary", b"x" * 262144]
        for message in messages:
            write, _ = _winapi.WriteFile(client, message, overlapped=True)
            assert _until(channel.poll) == [message]
            assert write.GetOverlappedResult(False) == (len(message), 0)
        channel.send(b"reply")
        read, _ = _winapi.ReadFile(client, 262144, overlapped=True)
        def pump_reply():
            channel.poll()
            return read.GetOverlappedResult(False)[1] == 0
        _until(pump_reply)
        assert read.getbuffer() == b"reply"
        with pytest.raises(OSError) as busy:
            _client(name)
        assert busy.value.winerror == 231
    finally:
        if client is not None:
            _winapi.CloseHandle(client)
        if channel is not None:
            channel.close()
            _until(channel.reap_close)
        server.close()
    replacement = _server(name)
    replacement.close()


def test_separate_process_client_exchanges_messages():
    name = _name()
    server = _server(name)
    script = r'''
import _winapi, sys, time
h = _winapi.CreateFile(sys.argv[1], 0xc0000000, 0, 0, 3, 0x40000000, 0)
try:
    _winapi.SetNamedPipeHandleState(h, 2, None, None)
    write, _ = _winapi.WriteFile(h, b'from-child', overlapped=True)
    if _winapi.WaitForSingleObject(write.event, 3000) != 0:
        raise TimeoutError('child write')
    assert write.GetOverlappedResult(False) == (10, 0)
    read, _ = _winapi.ReadFile(h, 262144, overlapped=True)
    if _winapi.WaitForSingleObject(read.event, 3000) != 0:
        raise TimeoutError('child read')
    assert read.GetOverlappedResult(False) == (10, 0)
    assert read.getbuffer() == b'from-owner'
    print('duplex-ok')
finally:
    _winapi.CloseHandle(h)
'''
    child = subprocess.Popen([sys.executable, "-c", script, name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    channel = None
    try:
        channel = _until(server.accept_nonblocking)
        assert _until(channel.poll) == [b"from-child"]
        channel.send(b"from-owner")
        channel.poll()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "duplex-ok"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=3)
        if channel is not None:
            channel.close()
            _until(channel.reap_close)
        server.close()


def _restricted_access_result(path):
    # Restriction SID Everyone is absent from the pipe ACL. Windows performs
    # both the ordinary SID check and the restricting-SID check. Running this
    # in a child process also isolates thread impersonation from pytest.
    script = r'''
import ctypes, json, _winapi, sys
from ctypes import wintypes as w
a = ctypes.WinDLL('advapi32', use_last_error=True)
k = ctypes.WinDLL('kernel32', use_last_error=True)
P = ctypes.c_void_p
class SID_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Sid', P), ('Attributes', w.DWORD)]
def bind(dll, name, result, args):
    f = getattr(dll, name); f.restype = result; f.argtypes = args; return f
open_token = bind(a, 'OpenProcessToken', w.BOOL, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)])
restrict = bind(a, 'CreateRestrictedToken', w.BOOL, [w.HANDLE,w.DWORD,w.DWORD,P,w.DWORD,P,w.DWORD,P,ctypes.POINTER(w.HANDLE)])
convert = bind(a, 'ConvertStringSidToSidW', w.BOOL, [w.LPCWSTR,ctypes.POINTER(P)])
impersonate = bind(a, 'ImpersonateLoggedOnUser', w.BOOL, [w.HANDLE])
revert = bind(a, 'RevertToSelf', w.BOOL, [])
free = bind(k, 'LocalFree', P, [P])
process = bind(k, 'GetCurrentProcess', w.HANDLE, [])
def check(ok):
    if not ok: raise ctypes.WinError(ctypes.get_last_error())
token = w.HANDLE(); restricted = w.HANDLE(); sid = P(); impersonating = False
errors = []
try:
    check(open_token(process(), 0x000E, ctypes.byref(token)))
    check(convert('S-1-1-0', ctypes.byref(sid)))
    restriction = SID_ATTRIBUTES(sid, 0)
    check(restrict(token, 1, 0, None, 0, None, 1, ctypes.byref(restriction), ctypes.byref(restricted)))
    check(impersonate(restricted)); impersonating = True
    for access in (0x80000000, 0xc0000000):
        try:
            h = _winapi.CreateFile(sys.argv[1], access, 0, 0, 3, 0x40000000, 0)
        except OSError as error:
            errors.append(error.winerror)
        else:
            _winapi.CloseHandle(h); errors.append(0)
finally:
    if impersonating: check(revert())
    if sid: free(sid)
    if restricted: _winapi.CloseHandle(restricted.value)
    if token: _winapi.CloseHandle(token.value)
h = _winapi.CreateFile(sys.argv[1], 0xc0000000, 0, 0, 3, 0x40000000, 0)
_winapi.CloseHandle(h)
print(json.dumps({'restricted_errors': errors, 'owner_allowed': True}))
'''
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_restricted_identity_denied_read_and_duplex_but_owner_allowed():
    name = _name()
    server = _server(name)
    try:
        assert _restricted_access_result(name) == {"restricted_errors": [5, 5], "owner_allowed": True}
    finally:
        server.close()


@contextmanager
def _raw_pipe(name, sddl=None, instances=1):
    """Independent Win32 fixture, including deliberately insecure descriptors."""
    import _winapi

    class Attributes(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    handle = None
    try:
        address = 0
        if sddl is not None:
            if not convert(sddl, 1, ctypes.byref(descriptor), None):
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            address = ctypes.addressof(attributes)
        handle = _winapi.CreateNamedPipe(name, 0x40000003, 14, instances, 262144, 262144, 0, address)
        yield handle
    finally:
        if handle is not None:
            _winapi.CloseHandle(handle)
        if descriptor:
            kernel.LocalFree(descriptor)


@pytest.mark.parametrize("sddl", [None, "D:P(A;;FA;;;WD)", "D:NO_ACCESS_CONTROL"])
def test_dacl_validator_rejects_default_everyone_and_null_acl(sddl):
    from bigqmt_bridge.pipe_security import verify_pipe_dacl

    with _raw_pipe(_name(), sddl) as handle:
        with pytest.raises(PermissionError):
            verify_pipe_dacl(handle)


def test_refuses_preexisting_pipe_even_when_it_has_spare_capacity():
    # An existing pipe must never be reused, regardless of its free instances.
    name = _name()
    with _raw_pipe(name, "D:P(A;;FA;;;WD)", instances=2):
        with pytest.raises(OSError) as error:
            _server(name)
        assert error.value.winerror == 5


def test_security_verification_failure_releases_created_pipe(monkeypatch):
    import bigqmt_bridge.pipe_server as module

    name = _name()
    def fail_verification(handle):
        raise PermissionError("injected descriptor read failure")
    with monkeypatch.context() as patch:
        patch.setattr(module, "verify_pipe_dacl", fail_verification)
        with pytest.raises(PermissionError):
            _server(name)
    replacement = _server(name)
    replacement.close()


def test_private_directory_is_created_with_inheritable_acl_and_files_verify(private_tmp_path):
    from bigqmt_bridge.pipe_security import protect_private_directory, verify_private_file

    directory = private_tmp_path / "new-session"
    report = protect_private_directory(directory)
    assert directory.is_dir()
    assert report["dacl_verified"] is True
    assert report["dacl_protected"] is True
    assert report["children_inherit"] is True
    assert report["allowed_principals"] == ["current_user", "SYSTEM"]
    for name in ("config.local.json", "bootstrap.py"):
        target = directory / name
        target.write_text("synthetic-placeholder", encoding="utf-8")
        checked = verify_private_file(target)
        assert checked["dacl_verified"] is True
        assert checked["allowed_principals"] == ["current_user", "SYSTEM"]
        assert "S-1-" not in json.dumps(checked)
    nested = directory / "nested"
    nested.mkdir()
    child = nested / "child.txt"
    child.write_text("synthetic-placeholder", encoding="utf-8")
    assert verify_private_file(child)["dacl_verified"] is True


def test_private_directory_refuses_existing_path_without_modifying_it(private_tmp_path):
    from bigqmt_bridge.pipe_security import protect_private_directory

    tmp_path = private_tmp_path
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("preserved", encoding="utf-8")
    with pytest.raises(FileExistsError):
        protect_private_directory(tmp_path)
    assert sentinel.read_text(encoding="utf-8") == "preserved"


def test_private_file_validator_rejects_broad_inherited_acl(private_tmp_path):
    from bigqmt_bridge.pipe_security import verify_private_file

    target = private_tmp_path / "unprotected.txt"
    target.write_text("synthetic-placeholder", encoding="utf-8")
    # Make this fixture independent of the host's temp-directory permissions.
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    assert convert("D:P(A;;FA;;;WD)", 1, ctypes.byref(descriptor), None)
    try:
        assert set_security(str(target), 4, descriptor)
    finally:
        kernel.LocalFree(descriptor)
    with pytest.raises(PermissionError):
        verify_private_file(target)


def test_private_file_read_is_denied_to_restricted_identity(private_tmp_path):
    from bigqmt_bridge.pipe_security import protect_private_directory, verify_private_file

    directory = private_tmp_path / "new-session"
    protect_private_directory(directory)
    target = directory / "config.local.json"
    target.write_text("synthetic-placeholder", encoding="utf-8")
    assert verify_private_file(target)["dacl_verified"] is True
    assert _restricted_access_result(target) == {"restricted_errors": [5, 5], "owner_allowed": True}


def test_private_paths_reject_real_junction_and_do_not_modify_target(private_tmp_path):
    import _winapi
    from bigqmt_bridge.pipe_security import protect_private_directory, verify_private_file

    tmp_path = private_tmp_path
    target = tmp_path / "target"
    protect_private_directory(target)
    config = target / "config.local.json"
    config.write_text("synthetic-placeholder", encoding="utf-8")
    junction = tmp_path / "alias"
    _winapi.CreateJunction(str(target), str(junction))
    try:
        with pytest.raises(ValueError, match="reparse"):
            protect_private_directory(junction)
        with pytest.raises(ValueError, match="reparse"):
            protect_private_directory(junction / "new-session")
        with pytest.raises(ValueError, match="reparse"):
            verify_private_file(junction / "config.local.json")
        assert not (target / "new-session").exists()
        assert config.read_text(encoding="utf-8") == "synthetic-placeholder"
    finally:
        junction.rmdir()  # Remove only the link, never recursively its target.


def test_private_file_validator_rejects_directory_and_missing_file(private_tmp_path):
    from bigqmt_bridge.pipe_security import protect_private_directory, verify_private_file

    directory = private_tmp_path / "new-session"
    protect_private_directory(directory)
    with pytest.raises(ValueError):
        verify_private_file(directory)
    with pytest.raises(FileNotFoundError):
        verify_private_file(directory / "missing.json")


@pytest.mark.parametrize("suffix", ["new\x00ignored", "new:alternate", "new.", "new "])
def test_private_directory_rejects_ambiguous_windows_names(private_tmp_path, suffix):
    from bigqmt_bridge.pipe_security import protect_private_directory

    tmp_path = private_tmp_path
    with pytest.raises(ValueError):
        protect_private_directory(tmp_path / suffix)
    assert not (tmp_path / "new").exists()


@contextmanager
def _native_descriptor(sddl):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    assert convert(sddl, 1, ctypes.byref(descriptor), None)
    try:
        yield descriptor
    finally:
        kernel.LocalFree(descriptor)


def _set_native_dacl(path, sddl):
    from bigqmt_bridge.pipe_security import _current_user_sid

    # Preserve the test process's own access even when its admin group is
    # disabled by UAC; never rely on administrator membership for cleanup.
    sddl += "(A;OICI;FA;;;" + _current_user_sid() + ")"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    with _native_descriptor(sddl) as descriptor:
        assert apply(str(path), 4 | 0x80000000, descriptor)


@pytest.mark.parametrize("rights", ["FA", "SD", "WD", "WO", "0x40", "0x1301bf"])
def test_private_creation_rejects_ancestor_mutation_rights(private_tmp_path, rights):
    from bigqmt_bridge.pipe_security import protect_private_directory

    ancestor = private_tmp_path / "shared"
    ancestor.mkdir()
    # BA/SYSTEM retain control for cleanup, while Everyone gets the precise
    # destructive right under test. The current user is the directory owner.
    _set_native_dacl(ancestor, "D:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;;" + rights + ";;;WD)")
    with pytest.raises(PermissionError, match="ancestor"):
        protect_private_directory(ancestor / "session")
    assert not (ancestor / "session").exists()


def test_private_verification_rejects_newly_weakened_ancestor(private_tmp_path):
    from bigqmt_bridge.pipe_security import protect_private_directory, verify_private_file

    ancestor = private_tmp_path / "ancestor"
    protect_private_directory(ancestor)
    session = ancestor / "session"
    protect_private_directory(session)
    target = session / "config.json"
    target.write_text("synthetic-placeholder", encoding="utf-8")
    _set_native_dacl(ancestor, "D:P(A;OICI;FA;;;WD)")
    with pytest.raises(PermissionError, match="ancestor"):
        verify_private_file(target)


def test_trusted_owner_required_even_when_directory_acl_is_narrow():
    from bigqmt_bridge.pipe_security import _verify_trusted_directory_descriptor

    # A native descriptor represents a foreign owner without creating accounts
    # or changing the ownership of any real filesystem object.
    with _native_descriptor("O:WDD:P(A;;FA;;;SY)") as descriptor:
        with pytest.raises(PermissionError, match="owner"):
            _verify_trusted_directory_descriptor(descriptor, is_root=False)


def test_create_subdirectory_and_inherit_only_rights_do_not_allow_ancestor_replacement():
    from bigqmt_bridge.pipe_security import _verify_trusted_directory_descriptor

    with _native_descriptor("O:SYD:P(A;;FA;;;SY)(A;;LC;;;WD)(A;OICIIO;FA;;;WD)") as descriptor:
        _verify_trusted_directory_descriptor(descriptor, is_root=False)


def test_owner_rights_ace_is_safe_only_after_owner_is_trusted():
    from bigqmt_bridge.pipe_security import _verify_trusted_directory_descriptor

    with _native_descriptor("O:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)") as descriptor:
        _verify_trusted_directory_descriptor(descriptor, is_root=False)
    with _native_descriptor("O:WDD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)") as descriptor:
        with pytest.raises(PermissionError, match="owner"):
            _verify_trusted_directory_descriptor(descriptor, is_root=False)


def test_delete_exemption_applies_only_to_volume_root():
    from bigqmt_bridge.pipe_security import _verify_trusted_directory_descriptor

    with _native_descriptor("O:SYD:P(A;;FA;;;SY)(A;;SD;;;WD)") as descriptor:
        _verify_trusted_directory_descriptor(descriptor, is_root=True)
        with pytest.raises(PermissionError, match="ancestor"):
            _verify_trusted_directory_descriptor(descriptor, is_root=False)
    with _native_descriptor("O:SYD:P(A;;FA;;;SY)(A;;0x40;;;WD)") as descriptor:
        with pytest.raises(PermissionError, match="ancestor"):
            _verify_trusted_directory_descriptor(descriptor, is_root=True)


def test_existing_delete_handle_blocks_private_creation(private_tmp_path):
    import _winapi
    from bigqmt_bridge.pipe_security import protect_private_directory

    handle = _winapi.CreateFile(str(private_tmp_path), 0x10000, 7, 0, 3, 0x02000000, 0)
    try:
        with pytest.raises(OSError) as error:
            protect_private_directory(private_tmp_path / "session")
        assert error.value.winerror == 32
        assert not (private_tmp_path / "session").exists()
    finally:
        _winapi.CloseHandle(handle)


def test_runtime_restricted_pipe_gate_denies_access_without_consuming_connection():
    import _winapi
    from bigqmt_bridge.pipe_security import verify_restricted_access

    name = _name()
    server = _server(name)
    client = None
    channel = None
    try:
        assert verify_restricted_access(name) == {"read_error": 5, "read_write_error": 5}
        assert server.accept_nonblocking() is None
        client = _client(name)
        channel = _until(server.accept_nonblocking)
    finally:
        if client is not None:
            _winapi.CloseHandle(client)
        if channel is not None:
            channel.close()
            _until(channel.reap_close)
        server.close()


def test_runtime_restricted_pipe_gate_refuses_to_pass_insecure_pipe():
    from bigqmt_bridge.pipe_security import verify_restricted_access

    name = _name()
    with _raw_pipe(name, "D:P(A;;FA;;;WD)"):
        with pytest.raises(PermissionError):
            verify_restricted_access(name)


def test_listener_cycles_release_native_pipe_and_event_handles():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    count_handles = kernel.GetProcessHandleCount
    count_handles.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count_handles.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    def count():
        value = wintypes.DWORD()
        assert count_handles(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value
    # Prime imports and API bindings before taking the resource baseline.
    first = _server()
    first.close()
    before = count()
    for _ in range(25):
        server = _server()
        assert server.accept_nonblocking() is None
        server.close()
    assert count() == before


def test_close_timeout_keeps_resources_until_safe_retry():
    import _winapi

    name = _name()
    server = _server(name)
    class DelayedCancellation:
        def WaitForSingleObject(self, event, timeout):
            return 258  # Simulate a kernel cancellation not yet acknowledged.
        def __getattr__(self, name):
            return getattr(_winapi, name)
    try:
        server._api = DelayedCancellation()
        with pytest.raises(TimeoutError):
            server.close()
        assert server.accept_nonblocking() is None
        with pytest.raises(OSError):
            _server(name)
    finally:
        server._api = _winapi
        server.close()
    replacement = _server(name)
    replacement.close()
