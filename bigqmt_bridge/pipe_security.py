"""Explicit Windows pipe and session-configuration ACLs for external Python.

The current process user and LocalSystem are the only allowed principals.
This does not isolate mutually untrusted processes running as the same user.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", wintypes.FILETIME),
        ("access", wintypes.FILETIME),
        ("write", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _AclHeader(ctypes.Structure):
    _fields_ = [("revision", wintypes.BYTE), ("reserved", wintypes.BYTE),
                ("size", wintypes.WORD), ("count", wintypes.WORD),
                ("reserved2", wintypes.WORD)]


class _AccessAce(ctypes.Structure):
    _fields_ = [("type", wintypes.BYTE), ("flags", wintypes.BYTE),
                ("size", wintypes.WORD), ("mask", wintypes.DWORD),
                ("sid_start", wintypes.DWORD)]


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise OSError("Windows named-pipe security is required")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    definitions = {
        "GetCurrentProcess": (kernel, wintypes.HANDLE, []),
        "CloseHandle": (kernel, wintypes.BOOL, [wintypes.HANDLE]),
        "LocalFree": (kernel, pointer, [pointer]),
        "OpenProcessToken": (advapi, wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]),
        "GetTokenInformation": (advapi, wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
        "ConvertSidToStringSidW": (advapi, wintypes.BOOL, [pointer, ctypes.POINTER(pointer)]),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (advapi, wintypes.BOOL, [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer]),
        "GetKernelObjectSecurity": (advapi, wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": (advapi, wintypes.BOOL, [pointer, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer), pointer]),
        "GetHandleInformation": (kernel, wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
        "CreateDirectoryW": (kernel, wintypes.BOOL, [wintypes.LPCWSTR, pointer]),
        "GetFileAttributesW": (kernel, wintypes.DWORD, [wintypes.LPCWSTR]),
        "CreateFileW": (kernel, wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, pointer, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
        "GetFileInformationByHandle": (kernel, wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(_FileInformation)]),
        "GetSecurityDescriptorOwner": (advapi, wintypes.BOOL, [pointer, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]),
        "GetSecurityDescriptorDacl": (advapi, wintypes.BOOL, [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]),
        "GetAce": (advapi, wintypes.BOOL, [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]),
        "GetDriveTypeW": (kernel, wintypes.UINT, [wintypes.LPCWSTR]),
    }
    functions = {}
    for name, (dll, result, args) in definitions.items():
        function = getattr(dll, name)
        function.restype, function.argtypes = result, args
        functions[name] = function
    return functions


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def _current_user_sid():
    api = _api()
    token = wintypes.HANDLE()
    _check(api["OpenProcessToken"](api["GetCurrentProcess"](), 0x0008, ctypes.byref(token)))
    try:
        length = wintypes.DWORD()
        api["GetTokenInformation"](token, 1, None, 0, ctypes.byref(length))
        if ctypes.get_last_error() != 122 or not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        _check(api["GetTokenInformation"](token, 1, buffer, length, ctypes.byref(length)))
        sid = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents.Sid
        text = ctypes.c_void_p()
        _check(api["ConvertSidToStringSidW"](sid, ctypes.byref(text)))
        try:
            return ctypes.wstring_at(text)
        finally:
            api["LocalFree"](text)
    finally:
        api["CloseHandle"](token)


class _ObjectSecurity:
    """Own SECURITY_ATTRIBUTES memory until Windows has copied it."""

    def __init__(self, *, children_inherit=False):
        self._descriptor = ctypes.c_void_p()
        # Protected DACL: no inherited, Everyone, anonymous or admin-group ACE.
        self._sid = _current_user_sid()
        principals = [self._sid] if self._sid == "S-1-5-18" else [self._sid, "SY"]
        flags = "OICI" if children_inherit else ""
        sddl = "D:P" + "".join("(A;" + flags + ";FA;;;" + sid + ")" for sid in principals)
        _check(_api()["ConvertStringSecurityDescriptorToSecurityDescriptorW"](
            sddl, 1, ctypes.byref(self._descriptor), None
        ))
        self._attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), self._descriptor, False
        )

    @property
    def address(self):
        if not self._descriptor:
            raise ValueError("security attributes are closed")
        return ctypes.addressof(self._attributes)

    def close(self):
        if self._descriptor:
            _api()["LocalFree"](self._descriptor)
            self._descriptor = ctypes.c_void_p()
            self._attributes.lpSecurityDescriptor = None


class PipeSecurity(_ObjectSecurity):
    """Own the non-inheritable SECURITY_ATTRIBUTES used to create a pipe."""

    def __init__(self):
        super().__init__()


def _read_descriptor(handle, information=4):
    api = _api()
    size = wintypes.DWORD()
    api["GetKernelObjectSecurity"](handle, information, None, 0, ctypes.byref(size))
    if ctypes.get_last_error() != 122 or not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(size.value)
    _check(api["GetKernelObjectSecurity"](handle, information, descriptor, size, ctypes.byref(size)))
    return descriptor


def _descriptor_dacl_text(descriptor):
    api = _api()
    text = ctypes.c_void_p()
    _check(api["ConvertSecurityDescriptorToStringSecurityDescriptorW"](
        descriptor, 1, 4, ctypes.byref(text), None
    ))
    try:
        return ctypes.wstring_at(text)
    finally:
        api["LocalFree"](text)


def _read_dacl(handle):
    return _descriptor_dacl_text(_read_descriptor(handle))


def _sid_text(sid):
    text = ctypes.c_void_p()
    _check(_api()["ConvertSidToStringSidW"](sid, ctypes.byref(text)))
    try:
        return ctypes.wstring_at(text)
    finally:
        _api()["LocalFree"](text)


def _trusted_principals():
    # Privileged administrators and Windows Modules Installer are outside the
    # nonadmin isolation boundary. Windows system-drive roots use the latter.
    return {_current_user_sid(), "S-1-5-18", "S-1-5-32-544",
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}


def _verify_trusted_owner(descriptor, trusted):
    owner = ctypes.c_void_p()
    defaulted = wintypes.BOOL()
    _check(_api()["GetSecurityDescriptorOwner"](descriptor, ctypes.byref(owner), ctypes.byref(defaulted)))
    if not owner or _sid_text(owner) not in trusted:
        raise PermissionError("private path has an untrusted owner")


def _verify_trusted_directory_descriptor(descriptor, *, is_root):
    trusted = _trusted_principals()
    _verify_trusted_owner(descriptor, trusted)
    # OWNER_RIGHTS applies only to the owner already checked above. Python's
    # private temporary directories use this ACE instead of spelling its SID.
    trusted.add("S-1-3-4")
    acl = ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    _check(_api()["GetSecurityDescriptorDacl"](
        descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
    ))
    if not present or not acl:
        raise PermissionError("private path ancestor has no restrictive DACL")
    # WRITE_DAC, WRITE_OWNER, FILE_DELETE_CHILD and GENERIC_ALL. DELETE on a
    # volume root cannot rename that root, but is unsafe on every other level.
    unsafe = 0x00040000 | 0x00080000 | 0x00000040 | 0x10000000
    if not is_root:
        unsafe |= 0x00010000
    header = ctypes.cast(acl, ctypes.POINTER(_AclHeader)).contents
    for index in range(header.count):
        address = ctypes.c_void_p()
        _check(_api()["GetAce"](acl, index, ctypes.byref(address)))
        ace = ctypes.cast(address, ctypes.POINTER(_AccessAce)).contents
        if ace.flags & 0x08 or ace.type == 1:  # INHERIT_ONLY / access denied.
            continue
        if ace.type != 0:
            raise PermissionError("private path ancestor uses unsupported access rules")
        if ace.mask & unsafe and _sid_text(address.value + 8) not in trusted:
            raise PermissionError("private path ancestor permits untrusted replacement")


def _verify_trusted_ancestors(path):
    api = _api()
    if api["GetDriveTypeW"](path.anchor) not in (2, 3, 6):
        raise ValueError("private path must be on a local filesystem")
    # Check root-to-leaf. Once a parent is trusted, nonadmins cannot substitute
    # its child while that child's own descriptor is checked. No write/delete
    # sharing also rejects conflicting open data-write/delete handles.
    for ancestor in reversed(path.parents):
        handle = api["CreateFileW"](str(ancestor), 0x00020081, 1, None, 3,
                                    0x02000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileInformation()
            _check(api["GetFileInformationByHandle"](handle, ctypes.byref(info)))
            if info.attributes & 0x400:
                raise ValueError("private paths must not contain a reparse point")
            if not info.attributes & 0x10:
                raise ValueError("private path ancestor is not a directory")
            _verify_trusted_directory_descriptor(
                _read_descriptor(handle, 5), is_root=ancestor.parent == ancestor
            )
        finally:
            api["CloseHandle"](handle)


def verify_pipe_dacl(handle):
    """Read back the actual descriptor and reject any broader or inherited ACL.

    The SDDL conversion is performed by Windows. Requiring only exact explicit
    allow ACEs also rejects a missing/null DACL, inherited ACEs and other rights.
    No SID, username, pipe name or process identifier is exposed in the report.
    """
    api = _api()
    actual = _read_dacl(handle)
    user = _current_user_sid()
    user_ace = "(A;;FA;;;" + ("SY" if user == "S-1-5-18" else user) + ")"
    system_ace = "(A;;FA;;;SY)"
    expected = {"D:P" + user_ace + system_ace, "D:P" + system_ace + user_ace}
    if user == "S-1-5-18":
        expected = {"D:P" + system_ace}
    if actual not in expected:
        raise PermissionError("named pipe DACL does not match the current-user-only policy")
    flags = wintypes.DWORD()
    _check(api["GetHandleInformation"](handle, ctypes.byref(flags)))
    if flags.value & 1:
        raise PermissionError("named pipe handle is inheritable")
    return {
        "dacl_verified": True,
        "dacl_protected": True,
        "allowed_principals": ["current_user", "SYSTEM"],
        "inheritable": False,
    }


def _checked_local_path(path, *, allow_missing=False):
    # Do not resolve(): resolving would silently follow a junction or symlink.
    if "\x00" in os.fspath(path):
        raise ValueError("private paths must not contain a null character")
    path = Path(path).absolute()
    if (not re.fullmatch(r"[A-Za-z]:", path.drive) or not path.is_absolute()
            or any(part in (".", "..") or part.endswith((".", " "))
                   or ":" in part for part in path.parts[1:])):
        raise ValueError("a local drive path without aliases is required")
    for component in reversed((path,) + tuple(path.parents)):
        flags = _api()["GetFileAttributesW"](str(component))
        if flags == 0xFFFFFFFF:
            error = ctypes.get_last_error()
            if allow_missing and component == path and error in (2, 3):
                return path
            raise ctypes.WinError(error)
        if flags & 0x400:
            raise ValueError("private paths must not contain a reparse point")
    return path


def _verify_private_path(path, *, directory):
    path = _checked_local_path(path)
    _verify_trusted_ancestors(path)
    api = _api()
    # READ_CONTROL | READ_ATTRIBUTES | READ_DATA/LIST_DIRECTORY. A metadata-only
    # handle does not enforce sharing checks; including READ_DATA does. We do
    # not read contents. Read-only sharing prevents rewriting/replacement while
    # this object and descriptor are being checked.
    handle = api["CreateFileW"](str(path), 0x00020081, 1, None, 3,
                                0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileInformation()
        _check(api["GetFileInformationByHandle"](handle, ctypes.byref(info)))
        if info.attributes & 0x400:
            raise ValueError("private paths must not contain a reparse point")
        if bool(info.attributes & 0x10) != directory:
            raise ValueError("expected a private directory" if directory else "expected a regular private file")
        descriptor = _read_descriptor(handle, 5)
        _verify_trusted_owner(descriptor, _trusted_principals())
        actual = _descriptor_dacl_text(descriptor)
    finally:
        api["CloseHandle"](handle)
    # Windows canonicalizes the descriptor. Accept only these allow ACEs;
    # inherited file ACEs are expected when files are created in the directory.
    match = re.fullmatch(r"D:(P?(?:AI)?)(\(A;[A-Z]*;FA;;;[A-Za-z0-9-]+\))+", actual)
    if match is None:
        raise PermissionError("private path DACL does not match the current-user-only policy")
    control = actual[2:actual.index("(")]
    entries = re.findall(r"\(A;([A-Z]*);FA;;;([A-Za-z0-9-]+)\)", actual)
    user = _current_user_sid()
    principals = {"SY", user if user != "S-1-5-18" else "SY"}
    allowed_flags = {"OICI"} if directory else {"", "ID"}
    if (len(entries) != len(principals) or {sid for _, sid in entries} != principals
            or any(flags not in allowed_flags for flags, _ in entries)
            or (directory and "P" not in control)):
        raise PermissionError("private path DACL does not match the current-user-only policy")
    report = {
        "dacl_verified": True,
        "dacl_protected": "P" in control,
        "allowed_principals": ["current_user", "SYSTEM"],
        "trusted_ancestors_verified": True,
    }
    if directory:
        report["children_inherit"] = True
    return report


def protect_private_directory(path):
    """Atomically create one NEW directory with a restricted inheritable DACL.

    Parents must already exist with trusted owners and no nonadmin replacement
    rights. Existing paths and all reparse points are
    rejected; no existing directory's permissions are changed. Write session
    configuration only after this succeeds, then call verify_private_file.
    A failed verification leaves the empty directory for explicit inspection.
    """
    path = _checked_local_path(path, allow_missing=True)
    _verify_trusted_ancestors(path)
    security = _ObjectSecurity(children_inherit=True)
    try:
        _check(_api()["CreateDirectoryW"](str(path), security.address))
    finally:
        security.close()
    return _verify_private_path(path, directory=True)


def verify_private_file(path):
    """Verify a regular file has only current-user/SYSTEM access; never read it."""
    return _verify_private_path(path, directory=False)


_RESTRICTED_PIPE_CHECK = r'''
import ctypes, json, sys, _winapi
from ctypes import wintypes as w
a = ctypes.WinDLL('advapi32', use_last_error=True)
k = ctypes.WinDLL('kernel32', use_last_error=True)
P = ctypes.c_void_p
class SidAttributes(ctypes.Structure):
    _fields_ = [('Sid', P), ('Attributes', w.DWORD)]
def bind(dll, name, result, args):
    f = getattr(dll, name)
    f.restype, f.argtypes = result, args
    return f
open_token = bind(a, 'OpenProcessToken', w.BOOL, [w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)])
restrict = bind(a, 'CreateRestrictedToken', w.BOOL, [w.HANDLE,w.DWORD,w.DWORD,P,w.DWORD,P,w.DWORD,P,ctypes.POINTER(w.HANDLE)])
convert = bind(a, 'ConvertStringSidToSidW', w.BOOL, [w.LPCWSTR,ctypes.POINTER(P)])
impersonate = bind(a, 'ImpersonateLoggedOnUser', w.BOOL, [w.HANDLE])
revert = bind(a, 'RevertToSelf', w.BOOL, [])
free = bind(k, 'LocalFree', P, [P])
process = bind(k, 'GetCurrentProcess', w.HANDLE, [])
def check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
token, restricted, sid = w.HANDLE(), w.HANDLE(), P()
impersonating = False
errors = []
try:
    check(open_token(process(), 0xE, ctypes.byref(token)))
    check(convert('S-1-1-0', ctypes.byref(sid)))
    restriction = SidAttributes(sid, 0)
    check(restrict(token, 1, 0, None, 0, None, 1, ctypes.byref(restriction), ctypes.byref(restricted)))
    check(impersonate(restricted))
    impersonating = True
    for access in (0x80000000, 0xC0000000):
        try:
            handle = _winapi.CreateFile(sys.argv[1], access, 0, 0, 3, 0x40000000, 0)
        except OSError as error:
            errors.append(error.winerror)
        else:
            _winapi.CloseHandle(handle)
            errors.append(0)
finally:
    if impersonating:
        check(revert())
    if sid:
        free(sid)
    if restricted:
        _winapi.CloseHandle(restricted.value)
    if token:
        _winapi.CloseHandle(token.value)
print(json.dumps(errors))
'''


def verify_restricted_access(pipe_name):
    """Require genuine restricted-token denial on an idle local pipe.

    Run before its legitimate client connects. Impersonation is confined to a
    bounded child process, reverted in finally, and no normal client connection
    is opened. A failed check is not proof of isolation and raises immediately.
    This tests a restricted identity, not a login to another Windows account.
    """
    if (not isinstance(pipe_name, str) or not pipe_name.startswith("\\\\.\\pipe\\")
            or "\x00" in pipe_name or len(pipe_name) <= 9):
        raise ValueError("a local named pipe is required")
    result = subprocess.run(
        [sys.executable, "-I", "-c", _RESTRICTED_PIPE_CHECK, pipe_name],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("restricted identity check could not complete")
    try:
        errors = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("restricted identity check returned invalid evidence") from None
    if errors != [5, 5]:
        raise PermissionError("restricted identity was not denied both pipe access modes")
    return {"read_error": errors[0], "read_write_error": errors[1]}
