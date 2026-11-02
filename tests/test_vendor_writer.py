import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))

try:
    import safe_raw_writer as w
except ModuleNotFoundError:
    w = None


def image_bytes():
    b = bytearray(4096)
    b[:3] = b'\xeb\x58\x90'
    b[11:13] = struct.pack('<H', 512)
    b[13] = 1
    b[14:16] = struct.pack('<H', 1)
    b[16] = 2
    b[32:36] = struct.pack('<I', 8)
    b[36:40] = struct.pack('<I', 1)
    b[82:90] = b'FAT32   '
    b[510:512] = b'\x55\xaa'
    b[512:] = bytes(i % 251 for i in range(3584))
    return bytes(b)


def disk(**overrides):
    d = dict(Number=7, Size=8192, LogicalSectorSize=512, PhysicalSectorSize=512,
             BusType='USB', IsSystem=False, IsBoot=False, IsOffline=False,
             IsReadOnly=False, UniqueId='usb-unique', SerialNumber='serial',
             FriendlyName='test-card', PartitionStyle='RAW', Partitions=[])
    d.update(overrides)
    return d


class ShortIO(io.BytesIO):
    def read(self, n=-1):
        return super().read(min(n, 37) if n >= 0 else 37)

    def write(self, data):
        return super().write(data[:29])


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(w, 'safe_raw_writer implementation is required')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / 'source.img'
        self.data = image_bytes()
        self.image.write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.target = Path(self.tmp.name) / 'card.bin'

    def test_real_file_write_and_full_reopen_readback(self):
        report = w.simulate(self.image, self.target, self.sha, write=True)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(report['readback_sha256'], self.sha)
        for name in ('prehash', 'write', 'flush', 'readback'):
            self.assertGreaterEqual(report['timings_seconds'][name], 0)

    def test_diagnose_never_creates_destination(self):
        report = w.simulate(self.image, self.target, self.sha, write=False)
        self.assertFalse(self.target.exists())
        self.assertEqual(report['mode'], 'diagnose')

    def test_wrong_expected_sha_never_creates_destination(self):
        with self.assertRaises(w.WriterError):
            w.simulate(self.image, self.target, '0' * 64, write=True)
        self.assertFalse(self.target.exists())

    def test_source_as_target_rejected(self):
        with self.assertRaises(w.WriterError):
            w.simulate(self.image, self.image, self.sha, write=True)
        self.assertEqual(self.image.read_bytes(), self.data)

    def test_existing_simulation_file_rejected(self):
        self.target.write_bytes(b'keep')
        with self.assertRaises(w.WriterError):
            w.simulate(self.image, self.target, self.sha, write=True)
        self.assertEqual(self.target.read_bytes(), b'keep')

    def test_dangerous_device_and_namespace_paths_rejected(self):
        for path in (r'\\.\PhysicalDrive7', r'\\?\GLOBALROOT\Device\Harddisk0',
                     r'\\?\Volume{abc}', r'\Device\Harddisk0', 'E:', 'CON',
                     'NUL.img', r'C:\temp\COM1'):
            with self.subTest(path=path), self.assertRaises(w.WriterError):
                w.validate_simulation_path(self.image, path)

    def test_short_reads_and_writes_complete(self):
        target = ShortIO()
        digest = w.copy_stream(ShortIO(self.data), target, len(self.data), 512)
        self.assertEqual(target.getvalue(), self.data)
        self.assertEqual(digest, self.sha)

    def test_zero_write_fails_at_exact_offset(self):
        class Broken(io.BytesIO):
            def write(self, data):
                return 0
        with self.assertRaises(w.WriterError) as c:
            w.copy_stream(io.BytesIO(self.data), Broken(), len(self.data), 512)
        self.assertEqual(c.exception.offset, 0)

    def test_short_source_eof_fails(self):
        with self.assertRaises(w.WriterError) as c:
            w.copy_stream(io.BytesIO(self.data[:700]), io.BytesIO(), 4096, 512)
        self.assertEqual(c.exception.offset, 700)

    def test_win32_error_offset_is_preserved(self):
        class Broken(io.BytesIO):
            def write(self, data):
                e = OSError('CRC')
                e.winerror = 23
                raise e
        with self.assertRaises(w.WriterError) as c:
            w.copy_stream(io.BytesIO(self.data), Broken(), 4096, 512)
        self.assertEqual(c.exception.winerror, 23)
        self.assertEqual(c.exception.offset, 0)

    def test_readback_corruption_detected(self):
        with self.assertRaises(w.WriterError) as caught:
            w.verify_stream(io.BytesIO(b'x' + self.data[1:]), 4096, self.sha, 512)
        self.assertIsNone(caught.exception.offset)

    def test_failure_closes_simulation_file_and_retains_partial_for_diagnosis(self):
        with mock.patch.object(w, 'copy_stream', side_effect=w.WriterError('injected', offset=0)):
            with self.assertRaises(w.WriterError):
                w.simulate(self.image, self.target, self.sha, write=True)
        self.target.unlink()  # Windows removal proves destination handle was closed.

    def test_source_change_is_rejected(self):
        original = w.copy_stream
        def change(src, dest, size, chunk, progress=None):
            changed = self.data[:-1] + b'X'
            return original(io.BytesIO(changed), dest, size, chunk, progress=progress)
        with mock.patch.object(w, 'copy_stream', side_effect=change):
            with self.assertRaises(w.WriterError):
                w.simulate(self.image, self.target, self.sha, write=True)

    def test_superfloppy_layout_recognized(self):
        self.assertEqual(w.inspect_layout(self.data[:512], len(self.data))['layout'], 'superfloppy')

    def test_mbr_layout_and_bounds(self):
        b = bytearray(512)
        b[510:] = b'\x55\xaa'
        b[446 + 4] = 0x0C
        struct.pack_into('<II', b, 446 + 8, 1, 7)
        self.assertEqual(w.inspect_layout(b, 4096)['layout'], 'MBR')
        struct.pack_into('<II', b, 446 + 8, 1, 8)
        with self.assertRaises(w.WriterError):
            w.inspect_layout(b, 4096)

    def test_invalid_boot_and_unaligned_image_rejected(self):
        self.image.write_bytes(b'bad' * 100)
        with self.assertRaises(w.WriterError):
            w.precheck_image(self.image, hashlib.sha256(self.image.read_bytes()).hexdigest())

    def test_unsafe_devices_rejected_without_opening_any_handle(self):
        cases = [dict(IsSystem=True), dict(IsBoot=True), dict(BusType='SATA'),
                 dict(BusType='Unknown'), dict(IsOffline=True), dict(IsReadOnly=True),
                 dict(Size=1024), dict(LogicalSectorSize=4096), dict(UniqueId='', SerialNumber='')]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(w.WriterError):
                w.validate_device(disk(**extra), 4096, {0}, 'ERASE 7 8192')

    def test_source_disk_and_imprecise_confirmation_rejected(self):
        for sources, confirm in (({7}, 'ERASE 7 8192'), ({0}, 'ERASE 7'), ({0}, None)):
            with self.assertRaises(w.WriterError):
                w.validate_device(disk(), 4096, sources, confirm)

    def test_raw_disk_allowed_and_guid_only_volume_supported(self):
        self.assertEqual(w.validate_device(disk(), 4096, {0}, 'ERASE 7 8192'), [])
        guid = '\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\'
        d = disk(PartitionStyle='MBR', Partitions=[dict(PartitionNumber=1, AccessPaths=[guid])])
        self.assertEqual(w.validate_device(d, 4096, {0}, 'ERASE 7 8192'), [guid.rstrip('\\')])

    def test_unexplained_partitions_refused(self):
        d = disk(PartitionStyle='MBR', Partitions=[dict(PartitionNumber=1, AccessPaths=['E:\\'])])
        with self.assertRaises(w.WriterError):
            w.validate_device(d, 4096, {0}, 'ERASE 7 8192')

    def test_identity_change_refused(self):
        with self.assertRaises(w.WriterError):
            w.require_same_device(disk(), disk(SerialNumber='different'))

    def test_cli_default_is_read_only_and_errors_nonzero(self):
        cmd = [sys.executable, str(Path(w.__file__)), '--image', str(self.image),
               '--simulate-file', str(self.target), '--expected-sha256', self.sha]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.target.exists())
        result = subprocess.run(cmd + ['--write', '--expected-sha256', '0' * 64],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_list_disks_wrapper_schema(self):
        output = io.StringIO()
        with mock.patch.object(w, 'enumerate_disks', return_value=[disk()]), mock.patch('sys.stdout', output):
            self.assertEqual(w.main(['--list-disks']), 0)
        parsed = json.loads(output.getvalue())
        self.assertIn('disks', parsed)
        d = parsed['disks'][0]
        self.assertEqual(d['number'], 7)
        self.assertEqual(d['size'], 8192)
        self.assertEqual(d['volumes'], [])

    def _fake_physical(self, fail=None, layout_change=False):
        events = []
        target = io.BytesIO()
        guid = '\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\'
        metadata = disk(PartitionStyle='MBR', Partitions=[dict(PartitionNumber=1, AccessPaths=[guid])])
        class Handle:
            def __init__(self, api, path, write=False):
                self.name = 'volume' if 'Volume{' in path else ('writer' if write else 'reader')
                events.append(('open', self.name))
                if self.name == 'reader':
                    target.seek(0)
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                events.append(('close', self.name))
            def ioctl(self, code):
                events.append(('ioctl', code))
                if fail == 'lock' and code == 0x90018:
                    raise w.WriterError('lock failed', winerror=5)
            def validate_open_device(self, d):
                events.append(('validate', self.name))
            def write(self, data):
                if fail == 'write':
                    raise w.WriterError('write CRC', offset=0, winerror=23)
                return target.write(data)
            def flush(self):
                events.append(('flush', self.name))
                if fail == 'flush':
                    raise w.WriterError('flush failed', offset=4096, winerror=21)
            def read(self, size):
                if fail == 'readback':
                    raise w.WriterError('readback failed', offset=0, winerror=23)
                return target.read(size)
        changed = disk(PartitionStyle='RAW', Partitions=[])
        enumerations = [[metadata]] * 3 + [[changed if layout_change else metadata]]
        with mock.patch.object(w, 'WinAPI'), mock.patch.object(w, 'RawHandle', Handle), \
             mock.patch.object(w, 'enumerate_disks', side_effect=enumerations), \
             mock.patch.object(w, 'source_disk_numbers', return_value={0}):
            if fail:
                with self.assertRaises(w.WriterError):
                    w.physical(self.image, 7, self.sha, 'ERASE 7 8192', True)
            else:
                result = w.physical(self.image, 7, self.sha, 'ERASE 7 8192', True)
                self.assertEqual(result['readback_sha256'], self.sha)
        return events

    def test_physical_locks_retained_through_close_reopen_readback(self):
        events = self._fake_physical()
        self.assertLess(events.index(('ioctl', 0x90018)), events.index(('open', 'writer')))
        self.assertLess(events.index(('close', 'writer')), events.index(('open', 'reader')))
        self.assertLess(events.index(('close', 'reader')), events.index(('close', 'volume')))

    def test_expected_layout_change_after_write_still_verifies(self):
        self._fake_physical(layout_change=True)

    def test_raw_enumeration_bypasses_no_partition_query(self):
        if os.name != 'nt':
            self.skipTest('PowerShell 5.1 runtime check requires Windows')
        # Override both commands locally; no real Get-Disk/Get-Partition runs.
        fake = "function Get-Disk { [pscustomobject]@{Number=7; PartitionStyle='RAW'} }; " \
               "function Get-Partition { throw 'No partitions found; must never query RAW' }; "
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 fake + w.ENUMERATE_PS], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['Disks'][0]['Partitions'], [])

    def test_physical_lock_write_flush_readback_failures_release_every_handle(self):
        for fail in ('lock', 'write', 'flush', 'readback'):
            with self.subTest(fail=fail):
                events = self._fake_physical(fail)
                for name in ('volume', 'writer', 'reader'):
                    self.assertEqual(events.count(('open', name)), events.count(('close', name)))
                if fail == 'lock':
                    self.assertNotIn(('open', 'writer'), events)

    def test_failed_write_reports_elapsed_phase_and_failure_offset(self):
        with mock.patch.object(w, 'copy_stream', side_effect=w.WriterError('CRC', 512, 23)):
            with self.assertRaises(w.WriterError) as caught:
                w.simulate(self.image, self.target, self.sha, write=True)
        self.assertTrue(hasattr(caught.exception, 'report'))
        report = caught.exception.report
        self.assertEqual(report['failure_phase'], 'write')
        self.assertGreaterEqual(report['timings_seconds']['write'], 0)

    def test_transfer_progress_reports_while_io_is_pending_and_stops_on_exception(self):
        self.assertTrue(hasattr(w, 'TransferProgress'))
        output = io.StringIO()
        with self.assertRaises(w.WriterError):
            with w.TransferProgress('write', 4096, stream=output, interval=0.01) as progress:
                progress.update(512)
                time.sleep(0.04)
                self.assertGreaterEqual(len(output.getvalue().splitlines()), 2)
                raise w.WriterError('injected')
        lines = output.getvalue().splitlines()
        self.assertTrue(any('phase=write' in line and 'offset=512' in line and 'MiB/s=' in line for line in lines))
        self.assertFalse(progress.thread.is_alive())
        self.assertIn('FAILED', lines[-1])


if __name__ == '__main__':
    unittest.main()
