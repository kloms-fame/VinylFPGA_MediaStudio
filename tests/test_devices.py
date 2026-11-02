import importlib
import io
import json
import tempfile
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class DeviceContract(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('studio.devices')
        except ImportError:
            self.fail('studio.devices and device-identity validation are not implemented')

    def test_changed_device_snapshot_is_rejected(self):
        disk = dict(Number=4, Size=128000000, UniqueId='card-a', SerialNumber='a',
                    IsSystem=False, IsBoot=False, IsOffline=False, IsReadOnly=False,
                    BusType='USB', LogicalSectorSize=512, PhysicalSectorSize=512,
                    FriendlyName='SD card', PartitionStyle='RAW', Partitions=[])
        with self.assertRaises(Exception):
            self.module.require_snapshot(disk, [dict(disk, UniqueId='card-b')])
        self.assertEqual(self.module.require_snapshot(disk, [disk]), disk)

    def test_writer_retains_source_system_and_sector_guards(self):
        writer = self.module.writer
        disk = dict(Number=4, Size=128000000, UniqueId='card-a', SerialNumber='a',
                    IsSystem=False, IsBoot=False, IsOffline=False, IsReadOnly=False,
                    BusType='USB', LogicalSectorSize=512, PhysicalSectorSize=512,
                    FriendlyName='SD card', PartitionStyle='RAW', Partitions=[])
        for changed, source in [(dict(disk, IsSystem=True), {0}), (disk, {4}),
                                (dict(disk, LogicalSectorSize=4096), {0})]:
            with self.assertRaises(writer.WriterError):
                writer.validate_device(changed, 1024, source, 'ERASE 4 128000000')

    def test_partial_writes_and_full_readback(self):
        writer = self.module.writer
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:17])
        payload = bytes(range(256))*8
        out = ShortWriter()
        digest = writer.copy_stream(io.BytesIO(payload), out, len(payload), chunk=128)
        self.assertEqual(out.getvalue(), payload)
        self.assertEqual(writer.verify_stream(io.BytesIO(payload), len(payload), digest), digest)
        with self.assertRaises(writer.WriterError):
            writer.verify_stream(io.BytesIO(bytes(len(payload))), len(payload), digest)

    def test_helper_retains_complete_identity_and_reports_progress(self):
        writer=self.module.writer
        disk=dict(Number=4,Size=128000000,UniqueId='card-a',SerialNumber='a',
                  IsSystem=False,IsBoot=False,IsOffline=False,IsReadOnly=False,
                  BusType='USB',LogicalSectorSize=512,PhysicalSectorSize=512,
                  FriendlyName='SD card',PartitionStyle='RAW',Partitions=[])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);image=root/'card.img';image.write_bytes(bytes(512))
            request=root/'request.json'
            request.write_text(json.dumps(dict(studio_write_request=1,image=str(image),sha256='0'*64,
                                               disk=disk,confirm='ERASE 4 128000000')))
            def simulated_physical(*args,**kwargs):
                writer.enumerate_disks()
                with writer.TransferProgress('write',1024) as progress:progress.update(1024)
                return dict(readback_sha256='0'*64)
            with mock.patch.object(writer,'enumerate_disks',return_value=[disk]),mock.patch.object(writer,'physical',side_effect=simulated_physical):
                self.assertEqual(self.module.write_request(request),0)
            path=request.with_suffix('.progress.json')
            self.assertTrue(path.is_file(),'elevated writer must expose progress to the parent UI')
            progress=json.loads(path.read_text())
            self.assertEqual(progress['phase'],'write');self.assertEqual(progress['offset'],1024)
            with mock.patch.object(writer,'enumerate_disks',side_effect=[[disk],[dict(disk,UniqueId='replaced')]]),mock.patch.object(writer,'physical',side_effect=simulated_physical):
                self.assertEqual(self.module.write_request(request),1)
            result=json.loads(request.with_suffix('.result.json').read_text())
            self.assertFalse(result['ok']);self.assertIn('identity',result['error'])


if __name__ == '__main__':
    unittest.main()
