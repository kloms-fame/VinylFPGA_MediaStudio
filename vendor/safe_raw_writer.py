"""Explicit Windows raw-card writer; read-only unless --write is supplied.

Uses buffered synchronous Win32 I/O with WRITE_THROUGH, not NO_BUFFERING.
Thus Python bytes need no physical alignment. All transfers remain sector
aligned on real disks. Volume locks stay held through flush, physical-handle
close/reopen and a full SHA256 readback. No dependency beyond Python stdlib.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager

CHUNK = 4 * 1024 * 1024


class WriterError(Exception):
    def __init__(self, message, offset=None, winerror=None):
        super().__init__(message)
        self.offset = offset
        self.winerror = winerror


def _io_error(exc, operation, offset):
    return WriterError(f'{operation}: {exc}', offset, getattr(exc, 'winerror', None))


class TransferProgress:
    """A reporting thread keeps stderr alive even if one device call stalls."""
    def __init__(self, phase, total, stream=None, interval=0.8):
        self.phase, self.total = phase, total
        self.stream = stream if stream is not None else sys.stderr
        self.interval = min(1.0, interval)
        self.offset = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def update(self, offset):
        self.offset = offset

    def _emit(self, status='RUNNING'):
        elapsed = max(time.perf_counter() - self.started, 0.000001)
        print(f'progress phase={self.phase} offset={self.offset} total={self.total} '
              f'MiB/s={self.offset / 1048576 / elapsed:.2f} status={status}',
              file=self.stream, flush=True)

    def _run(self):
        while not self.stopped.wait(self.interval):
            self._emit()

    def __enter__(self):
        self.started = time.perf_counter()
        self._emit()
        self.thread.start()
        return self

    def __exit__(self, exc_type, *exc):
        self.stopped.set()
        self.thread.join()
        self._emit('FAILED' if exc_type else 'DONE')


def copy_stream(source, target, size, chunk=CHUNK, progress=None):
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            data = source.read(min(chunk, size - offset))
        except OSError as exc:
            raise _io_error(exc, 'source read', offset) from exc
        if not data:
            raise WriterError('unexpected source EOF', offset)
        digest.update(data)
        done = 0
        while done < len(data):
            try:
                written = target.write(memoryview(data)[done:])
            except OSError as exc:
                raise _io_error(exc, 'target write', offset + done) from exc
            if not isinstance(written, int) or written <= 0 or written > len(data) - done:
                raise WriterError('invalid or zero-length write', offset + done)
            done += written
            if progress:
                progress.update(offset + done)
        offset += len(data)
    return digest.hexdigest()


def verify_stream(source, size, expected_sha256, chunk=CHUNK, progress=None):
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            data = source.read(min(chunk, size - offset))
        except OSError as exc:
            raise _io_error(exc, 'readback', offset) from exc
        if not data:
            raise WriterError('unexpected readback EOF', offset)
        digest.update(data)
        offset += len(data)
        if progress:
            progress.update(offset)
    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        raise WriterError(f'whole-image SHA256 mismatch; first bad byte unknown: expected {expected_sha256}, actual {actual}')
    return actual


def inspect_layout(boot, size):
    if size < 512 or size % 512 or len(boot) != 512 or boot[510:512] != b'\x55\xaa':
        raise WriterError('image must have aligned size and valid boot-sector signature')
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    if boot[0] in (0xEB, 0xE9) and bps == 512 and spc in (1, 2, 4, 8, 16, 32, 64, 128):
        total = struct.unpack_from('<H', boot, 19)[0] or struct.unpack_from('<I', boot, 32)[0]
        if not total or total * 512 > size or boot[16] not in (1, 2):
            raise WriterError('invalid FAT superfloppy BPB/size')
        return dict(layout='superfloppy', filesystem_bytes=total * 512)
    parts = []
    for slot in range(4):
        entry = boot[446 + 16 * slot:462 + 16 * slot]
        kind = entry[4]
        start, count = struct.unpack_from('<II', entry, 8)
        if not kind:
            if start or count:
                raise WriterError('unexplained empty MBR partition entry')
            continue
        if entry[0] not in (0, 0x80) or kind not in (0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E):
            raise WriterError('only simple FAT MBR partitions are supported; GPT/extended refused')
        if not start or not count or (start + count) * 512 > size:
            raise WriterError('MBR partition extends beyond image')
        if any(start < p['start'] + p['count'] and p['start'] < start + count for p in parts):
            raise WriterError('overlapping MBR partitions')
        parts.append(dict(type=kind, start=start, count=count))
    if not parts:
        raise WriterError('unrecognized image layout')
    return dict(layout='MBR', partitions=parts)


def _stat_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def precheck_image(image, expected_sha256):
    path = Path(image).resolve(strict=True)
    if not path.is_file() or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256 or ''):
        raise WriterError('regular image file and explicit 64-digit SHA256 are required')
    started = time.perf_counter()
    with path.open('rb') as source:
        stat = os.fstat(source.fileno())
        layout = inspect_layout(source.read(512), stat.st_size)
        source.seek(0)
        with TransferProgress('prehash', stat.st_size) as progress:
            sha = verify_stream(source, stat.st_size, expected_sha256, progress=progress)
        if _stat_signature(os.fstat(source.fileno())) != _stat_signature(stat):
            raise WriterError('source changed during SHA256 precheck')
    return dict(image=str(path), size_bytes=stat.st_size, expected_sha256=sha,
                layout=layout, source_signature=_stat_signature(stat),
                timings_seconds=dict(prehash=time.perf_counter() - started))


def validate_simulation_path(image, target):
    raw = str(target)
    normalized = raw.replace('/', '\\')
    if normalized.startswith(('\\\\', '\\Device\\')) or re.fullmatch(r'[a-zA-Z]:\\?', normalized):
        raise WriterError('simulation requires a normal local file path, never a device/root/UNC path')
    for component in normalized.split('\\'):
        basename = component.rstrip(' .').split('.')[0].upper()
        if basename in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(COM|LPT)[0-9¹²³]', basename):
            raise WriterError('reserved Windows device name refused')
        if ':' in component and not re.fullmatch(r'[a-zA-Z]:', component):
            raise WriterError('alternate data streams are refused')
    path = Path(target).resolve()
    if path == Path(image).resolve() or (path.exists() and os.path.samefile(image, path)):
        raise WriterError('source cannot be the destination')
    if path.exists():
        raise WriterError('simulation refuses an existing destination; choose a new temporary file')
    if not path.parent.is_dir():
        raise WriterError('simulation destination directory does not exist')
    return path


def _finish_report(report, mode):
    report['mode'] = mode
    report['throughput_mib_s'] = {
        phase: report['size_bytes'] / 1048576 / seconds
        for phase, seconds in report['timings_seconds'].items()
        if seconds > 0 and phase in ('prehash', 'write', 'readback')}
    return report


@contextmanager
def timed_phase(report, name):
    started = time.perf_counter()
    try:
        yield
    except (WriterError, OSError) as exc:
        report['failure_phase'] = name
        exc.report = report
        raise
    finally:
        report['timings_seconds'][name] = time.perf_counter() - started


def _check_source(source, report):
    if _stat_signature(os.fstat(source.fileno())) != tuple(report['source_signature']):
        raise WriterError('source changed since precheck; refusing write')


def simulate(image, target, expected_sha256, write=False):
    target = validate_simulation_path(image, target)
    report = precheck_image(image, expected_sha256)
    report['target'] = str(target)
    if not write:
        return _finish_report(report, 'diagnose')
    with Path(report['image']).open('rb') as source, target.open('xb', buffering=0) as dest:
        _check_source(source, report)
        with timed_phase(report, 'write'), TransferProgress('write', report['size_bytes']) as progress:
            copied_sha = copy_stream(source, dest, report['size_bytes'], CHUNK, progress=progress)
            if copied_sha != report['expected_sha256']:
                raise WriterError('source bytes changed after precheck; target is invalid', report['size_bytes'])
            _check_source(source, report)
        with timed_phase(report, 'flush'):
            dest.flush()
            os.fsync(dest.fileno())
    with timed_phase(report, 'readback'), target.open('rb', buffering=0) as readback, \
         TransferProgress('readback', report['size_bytes']) as progress:
        report['readback_sha256'] = verify_stream(readback, report['size_bytes'], copied_sha, progress=progress)
    return _finish_report(report, 'simulate-write-verified')


# ConvertTo-Json works on Windows PowerShell 5.1; AccessPaths include volume
# GUIDs even when there is no drive letter. Errors stop enumeration, never
# masquerade as a RAW disk with no volumes.
ENUMERATE_PS = r"""
$ErrorActionPreference = 'Stop'
$disks = @(Get-Disk | ForEach-Object {
  $d = $_
  if ([string]$d.PartitionStyle -eq 'RAW') {
    $parts = @()
  } else {
    $parts = @(Get-Partition -DiskNumber $d.Number | ForEach-Object {
      [pscustomobject]@{PartitionNumber=$_.PartitionNumber; AccessPaths=@($_.AccessPaths)}
    })
  }
  [pscustomobject]@{
    Number=$d.Number; Size=$d.Size; LogicalSectorSize=$d.LogicalSectorSize
    PhysicalSectorSize=$d.PhysicalSectorSize; BusType=[string]$d.BusType
    IsBoot=$d.IsBoot; IsSystem=$d.IsSystem; IsOffline=$d.IsOffline
    IsReadOnly=$d.IsReadOnly; UniqueId=$d.UniqueId; SerialNumber=$d.SerialNumber
    FriendlyName=$d.FriendlyName; PartitionStyle=[string]$d.PartitionStyle; Partitions=$parts
  }
})
[pscustomobject]@{Disks=$disks} | ConvertTo-Json -Depth 8 -Compress
"""


def enumerate_disks():
    if os.name != 'nt':
        raise WriterError('physical disk enumeration is Windows-only')
    # Emit UTF-8 explicitly rather than relying on PowerShell console encoding.
    script = '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false);\n' + ENUMERATE_PS
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    if result.returncode:
        raise WriterError('read-only Get-Disk/Get-Partition failed: ' + result.stderr.strip())
    try:
        obj = json.loads(result.stdout.lstrip('\ufeff'))
        disks = obj['Disks']
        if not isinstance(disks, list):
            raise ValueError('Disks must be a list')
        return disks
    except (ValueError, KeyError, TypeError) as exc:
        raise WriterError('invalid PowerShell disk metadata') from exc


def get_disk(disks, number):
    matches = [d for d in disks if d.get('Number') == number]
    if len(matches) != 1:
        raise WriterError('target disk number is absent or ambiguous')
    return matches[0]


def disk_summary(disk):
    fields = dict(number='Number', friendly_name='FriendlyName', serial_number='SerialNumber',
        unique_id='UniqueId', size='Size', bus_type='BusType', is_boot='IsBoot', is_system='IsSystem',
        is_read_only='IsReadOnly', is_offline='IsOffline', logical_sector_size='LogicalSectorSize',
        physical_sector_size='PhysicalSectorSize', partition_style='PartitionStyle')
    summary = {key: disk.get(source) for key, source in fields.items()}
    summary['volumes'] = [p for part in disk.get('Partitions', []) for p in part.get('AccessPaths', [])]
    summary['partitions'] = disk.get('Partitions', [])
    return summary


def identity(disk, compare_layout=True):
    fields = ('Number', 'Size', 'LogicalSectorSize', 'PhysicalSectorSize', 'BusType', 'UniqueId',
        'SerialNumber', 'FriendlyName', 'IsBoot', 'IsSystem', 'IsOffline', 'IsReadOnly')
    if compare_layout:
        fields += ('PartitionStyle', 'Partitions')
    return tuple(disk.get(k) for k in fields)


def require_same_device(before, after, compare_layout=True):
    if identity(before, compare_layout) != identity(after, compare_layout):
        raise WriterError('device identity/state/volume set changed; restart diagnosis')


def validate_device(disk, image_size, source_disks, confirm, require_confirm=True):
    if disk.get('IsSystem') is not False or disk.get('IsBoot') is not False:
        raise WriterError('system/boot disk or missing safety metadata refused')
    if disk.get('IsOffline') is not False or disk.get('IsReadOnly') is not False:
        raise WriterError('offline/read-only disk refused')
    if disk.get('BusType') not in ('USB', 'SD', 'MMC'):
        raise WriterError('only explicit USB/SD/MMC disks are permitted')
    if not str(disk.get('UniqueId') or '').strip() and not str(disk.get('SerialNumber') or '').strip():
        raise WriterError('device has no stable UniqueId or serial number')
    if not source_disks or disk.get('Number') in source_disks:
        raise WriterError('source disk unresolved or same as target')
    sector = disk.get('LogicalSectorSize')
    if sector != 512 or image_size % sector:
        raise WriterError('VinylFPGA images require 512-byte logical sectors')
    if not isinstance(disk.get('Size'), int) or disk['Size'] < image_size:
        raise WriterError('disk capacity is smaller than image')
    if require_confirm and confirm != f"ERASE {disk['Number']} {disk['Size']}":
        raise WriterError('exact confirmation required: ERASE <disk-number> <size-bytes>')
    partitions = disk.get('Partitions')
    if not isinstance(partitions, list) or disk.get('PartitionStyle') not in ('RAW', 'MBR', 'GPT'):
        raise WriterError('partition enumeration missing or unexplained')
    volumes = []
    for part in partitions:
        paths = part.get('AccessPaths')
        if not isinstance(paths, list):
            raise WriterError('partition access paths missing')
        guids = [p.rstrip('\\') for p in paths if isinstance(p, str) and
                 re.fullmatch(r'\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\?', p)]
        if len(guids) != 1:
            raise WriterError('every existing partition must resolve to exactly one lockable volume GUID')
        volumes.extend(guids)
    return sorted(set(volumes))


class WinAPI:
    def __init__(self):
        if os.name != 'nt':
            raise WriterError('Win32 physical writing is Windows-only')
        self.dll = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'ReadFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'WriteFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'FlushFileBuffers': ([wintypes.HANDLE], wintypes.BOOL),
            'DeviceIoControl': ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'GetVolumePathNameW': ([wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL),
            'GetVolumeNameForVolumeMountPointW': ([wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes, fn.restype = args, result

    def check(self, success, operation):
        if not success:
            code = ctypes.get_last_error()
            raise WriterError(f'{operation}: {ctypes.FormatError(code).strip()}', winerror=code)

    def volume_for_file(self, path):
        mount, guid = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(128)
        self.check(self.dll.GetVolumePathNameW(str(Path(path).resolve()), mount, len(mount)), 'source volume path')
        self.check(self.dll.GetVolumeNameForVolumeMountPointW(mount.value, guid, len(guid)), 'source volume GUID')
        return guid.value.casefold()


class RawHandle:
    def __init__(self, api, path, write=False):
        self.api = api
        self.handle = api.dll.CreateFileW(path, 0x80000000 | (0x40000000 if write else 0),
                                        3, None, 3, 0x80000000 if write else 0, None)
        if self.handle == ctypes.c_void_p(-1).value:
            api.check(False, f'open {path}')
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.api.check(self.api.dll.CloseHandle(handle), 'CloseHandle')

    def ioctl(self, code, outsize=0):
        output = ctypes.create_string_buffer(outsize) if outsize else None
        count = wintypes.DWORD()
        self.api.check(self.api.dll.DeviceIoControl(self.handle, code, None, 0, output,
                        outsize, ctypes.byref(count), None), f'DeviceIoControl 0x{code:x}')
        return output.raw[:count.value] if output is not None else b''

    def validate_open_device(self, disk):
        number = self.ioctl(0x2D1080, 12)  # IOCTL_STORAGE_GET_DEVICE_NUMBER
        length = self.ioctl(0x7405C, 8)   # IOCTL_DISK_GET_LENGTH_INFO
        if len(number) != 12 or len(length) != 8:
            raise WriterError('incomplete opened-device IOCTL metadata')
        device_type, device_number, _ = struct.unpack('<III', number)
        if device_type != 7 or device_number != disk['Number'] or struct.unpack('<q', length)[0] != disk['Size']:
            raise WriterError('opened physical handle does not match selected disk number/capacity')

    def read(self, size):
        buf, count = ctypes.create_string_buffer(size), wintypes.DWORD()
        try:
            self.api.check(self.api.dll.ReadFile(self.handle, buf, size, ctypes.byref(count), None), 'ReadFile')
        except WriterError as exc:
            exc.offset = self.offset
            raise
        self.offset += count.value
        if count.value % 512:
            raise WriterError('physical read returned an unaligned short transfer', self.offset)
        return buf.raw[:count.value]

    def write(self, data):
        payload, count = bytes(data), wintypes.DWORD()
        if len(payload) % 512 or self.offset % 512:
            raise WriterError('unaligned physical write refused', self.offset)
        buf = ctypes.create_string_buffer(payload)
        try:
            self.api.check(self.api.dll.WriteFile(self.handle, buf, len(payload), ctypes.byref(count), None), 'WriteFile')
        except WriterError as exc:
            exc.offset = self.offset
            raise
        self.offset += count.value
        if count.value % 512:
            raise WriterError('physical write returned an unaligned short transfer', self.offset)
        return count.value

    def flush(self):
        try:
            self.api.check(self.api.dll.FlushFileBuffers(self.handle), 'FlushFileBuffers')
        except WriterError as exc:
            exc.offset = self.offset
            raise


def source_disk_numbers(api, image, disks):
    guid = api.volume_for_file(image)
    matches = {d['Number'] for d in disks for p in d.get('Partitions', [])
               for a in p.get('AccessPaths', []) if isinstance(a, str) and a.casefold() == guid}
    if len(matches) != 1:
        raise WriterError('source volume cannot be mapped uniquely to a physical disk')
    return matches


def physical(image, disk_number, expected_sha256, confirm=None, write=False):
    report = precheck_image(image, expected_sha256)
    api = WinAPI()
    disks = enumerate_disks()
    selected = get_disk(disks, disk_number)
    sources = source_disk_numbers(api, report['image'], disks)
    volumes = validate_device(selected, report['size_bytes'], sources, confirm, require_confirm=write)
    report['device'] = selected
    report['lock_volumes'] = volumes
    if not write:
        return _finish_report(report, 'diagnose')
    # Source stays open for the complete operation; its metadata and streamed
    # SHA detect changes. Locks are acquired before any physical write handle.
    with ExitStack() as locks, Path(report['image']).open('rb') as source:
        _check_source(source, report)
        require_same_device(selected, get_disk(enumerate_disks(), disk_number))
        for guid in volumes:
            volume = locks.enter_context(RawHandle(api, guid, write=True))
            volume.ioctl(0x90018)  # FSCTL_LOCK_VOLUME, no writes if any lock fails
            volume.ioctl(0x90020)  # FSCTL_DISMOUNT_VOLUME
        path = rf'\\.\PhysicalDrive{disk_number}'
        with RawHandle(api, path, write=True) as dest:
            dest.validate_open_device(selected)
            latest_disks = enumerate_disks()
            latest = get_disk(latest_disks, disk_number)
            require_same_device(selected, latest)
            validate_device(latest, report['size_bytes'], source_disk_numbers(api, report['image'], latest_disks), confirm)
            _check_source(source, report)
            with timed_phase(report, 'write'), TransferProgress('write', report['size_bytes']) as progress:
                copied = copy_stream(source, dest, report['size_bytes'], progress=progress)
                if copied != report['expected_sha256']:
                    raise WriterError('source changed during write; card is invalid', report['size_bytes'])
                _check_source(source, report)
            with timed_phase(report, 'flush'):
                dest.flush()
        # Reopen the physical handle and read the complete written range while
        # volume locks stay held. Reopening buffered I/O does not prove cache
        # bypass or persistence after removal; eject/reinsert acceptance remains.
        with timed_phase(report, 'readback'), RawHandle(api, path) as readback, \
             TransferProgress('readback', report['size_bytes']) as progress:
            readback.validate_open_device(selected)
            after_disks = enumerate_disks()
            require_same_device(selected, get_disk(after_disks, disk_number), compare_layout=False)
            if source_disk_numbers(api, report['image'], after_disks) != sources:
                raise WriterError('source disk mapping changed during operation')
            report['readback_sha256'] = verify_stream(readback, report['size_bytes'], copied, progress=progress)
    return _finish_report(report, 'physical-write-verified')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-disks', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--disk-number', type=int)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--confirm')
    parser.add_argument('--diagnose', action='store_true')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--simulate-file')
    args = parser.parse_args(argv)
    try:
        if args.list_disks:
            if args.write:
                raise WriterError('--list-disks cannot be combined with --write')
            report = dict(disks=[disk_summary(d) for d in enumerate_disks()])
        else:
            if not args.image or not args.expected_sha256:
                raise WriterError('--image and --expected-sha256 are required')
            if args.diagnose and args.write:
                raise WriterError('--diagnose and --write are mutually exclusive')
            if args.simulate_file:
                if args.disk_number is not None:
                    raise WriterError('simulation and physical disk target are mutually exclusive')
                report = simulate(args.image, args.simulate_file, args.expected_sha256, args.write)
            else:
                if args.disk_number is None or args.disk_number < 0:
                    raise WriterError('explicit --disk-number required; no disk is assumed to be a card')
                report = physical(args.image, args.disk_number, args.expected_sha256, args.confirm, args.write)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except (WriterError, OSError, ValueError, subprocess.SubprocessError) as exc:
        failure = dict(status='FAILED', message=str(exc), failure_offset=getattr(exc, 'offset', None),
                       win32_error=getattr(exc, 'winerror', None))
        if hasattr(exc, 'report'):
            failure['report'] = _finish_report(exc.report, 'failed')
        print(json.dumps(failure, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
