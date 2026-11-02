"""Explicit physical-card operations, isolated from the web request process."""
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def bundle_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


_spec = importlib.util.spec_from_file_location('studio_safe_raw_writer', bundle_root()/'vendor/safe_raw_writer.py')
writer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writer)


def require_snapshot(expected, current, compare_layout=True):
    if not isinstance(expected, dict) or not isinstance(expected.get('Number'), int):
        raise ValueError('请选择具体 SD 卡，并重新读取设备信息')
    selected = writer.get_disk(current, expected['Number'])
    writer.require_same_device(expected, selected, compare_layout=compare_layout)
    return selected


def diagnose(image, digest, expected):
    require_snapshot(expected, writer.enumerate_disks())
    return writer.physical(image, expected['Number'], digest, write=False)


def write_request(request_path):
    """Entry point in the elevated helper; the UI never opens a physical write handle."""
    request_path = Path(request_path).resolve(strict=True)
    response_path = request_path.with_suffix('.result.json')
    try:
        request = json.loads(request_path.read_text(encoding='utf-8'))
        if request.get('studio_write_request') != 1:
            raise ValueError('无效写卡请求')
        image = Path(request['image']).resolve(strict=True)
        expected = request['disk']
        if request['confirm'] != f"ERASE {expected['Number']} {expected['Size']}":
            raise ValueError('磁盘确认文字不匹配')
        require_snapshot(expected, writer.enumerate_disks())
        original_enumerate = writer.enumerate_disks
        original_progress = writer.TransferProgress

        class ReportingProgress(original_progress):
            def _emit(self, status='RUNNING'):
                if self.stream is not None:
                    super()._emit(status)
                target = request_path.with_suffix('.progress.json')
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(phase=self.phase, offset=self.offset,
                                                       total=self.total, status=status)), encoding='utf-8')
                temporary.replace(target)

        def guarded_enumerate():
            disks = original_enumerate()
            require_snapshot(expected, disks, compare_layout=False)
            return disks
        writer.enumerate_disks = guarded_enumerate
        writer.TransferProgress = ReportingProgress
        try:
            report = writer.physical(image, expected['Number'], request['sha256'], request['confirm'], write=True)
        finally:
            writer.enumerate_disks = original_enumerate
            writer.TransferProgress = original_progress
        result = dict(ok=True, report=report)
    except Exception as exc:
        result = dict(ok=False, error=str(exc))
    temporary = response_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(response_path)
    return 0 if result['ok'] else 1


def elevated_write(image, digest, expected, confirm, request_dir, progress):
    if os.name != 'nt':
        raise ValueError('物理写卡仅支持 Windows')
    # Reject unexpected metadata before UAC. No write handle has been opened here.
    require_snapshot(expected, writer.enumerate_disks())
    if confirm != f"ERASE {expected['Number']} {expected['Size']}":
        raise ValueError('请准确输入界面显示的磁盘确认文字')
    if expected.get('IsBoot') is not False or expected.get('IsSystem') is not False:
        raise ValueError('不能选择启动盘或系统盘')
    request_dir = Path(request_dir)
    request_dir.mkdir(parents=True, exist_ok=True)
    request = request_dir/(uuid.uuid4().hex+'.json')
    request.write_text(json.dumps(dict(studio_write_request=1, image=str(Path(image).resolve()),
                                      sha256=digest, disk=expected, confirm=confirm)), encoding='utf-8')
    response = request.with_suffix('.result.json')
    executable = sys.executable
    args = ['--writer', str(request)]
    if not getattr(sys, 'frozen', False):
        args.insert(0, str(bundle_root()/'src/launcher.py'))

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR),
                    ('lpParameters', wintypes.LPCWSTR), ('lpDirectory', wintypes.LPCWSTR),
                    ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR),
                    ('hkeyClass', wintypes.HKEY), ('dwHotKey', wintypes.DWORD),
                    ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile = 'runas', executable
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory, info.nShow = str(bundle_root()), 0
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), '未启动管理员写卡程序；可能取消了 UAC')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        while kernel.WaitForSingleObject(info.hProcess, 500) == 258:
            try:
                state = json.loads(request.with_suffix('.progress.json').read_text(encoding='utf-8'))
                fraction = min(1.0, state['offset'] / max(1, state['total']))
                label = {'prehash': '校验源镜像', 'write': '写入 SD 卡', 'readback': '完整回读校验'}.get(state['phase'], state['phase'])
                progress(state['phase'], fraction, f"{label}：{state['offset']:,} / {state['total']:,} 字节；请保持 SD 卡连接")
            except (OSError, ValueError, KeyError, TypeError):
                progress('physical-write', 0, '管理员程序正在预检、写入及完整回读；请保持 SD 卡连接')
        if not response.is_file():
            raise ValueError('写卡程序异常退出，未生成成功报告；请勿将该卡视为完成')
        result = json.loads(response.read_text(encoding='utf-8'))
        if not result['ok']:
            raise ValueError(result['error'])
        return result['report']
    finally:
        kernel.CloseHandle(info.hProcess)
