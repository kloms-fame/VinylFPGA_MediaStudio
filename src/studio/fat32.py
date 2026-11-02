"""Minimal fresh FAT32 superfloppy builder with contiguous 8.3 root files."""
import hashlib
import math
import re
import struct
from pathlib import Path


class Fat32Builder:
    def __init__(self, path, size_mib=64):
        self.path = Path(path)
        if not isinstance(size_mib, int) or size_mib < 64 or size_mib > 32768:
            raise ValueError('FAT32 镜像容量需为 64–32768 MiB')
        self.total_sectors = size_mib * 2048
        self.spc = 1
        while self.total_sectors // self.spc > 1000000: self.spc *= 2
        self.reserved = 32
        # A conservative upper bound avoids one-sector fixed-point oscillation
        # where shrinking FAT increases the number of data clusters again.
        self.fat_sectors = math.ceil((self.total_sectors // self.spc + 2) / 128)
        self.cluster_count = (self.total_sectors - self.reserved - 2 * self.fat_sectors) // self.spc
        if self.cluster_count < 65525: raise ValueError('簇数不足以构成 FAT32')
        self.data_lba = self.reserved + 2 * self.fat_sectors
        self.fat = bytearray(self.fat_sectors * 512)
        struct.pack_into('<III', self.fat, 0, 0x0FFFFFF8, 0xFFFFFFFF, 0x0FFFFFFF)
        self.next_cluster, self.entries, self.records = 3, [], []
        self.closed = False
        self.fh = self.path.open('xb')
        self.fh.truncate(self.total_sectors * 512)
        boot = bytearray(512)
        boot[:11] = b'\xEB\x58\x90VINYL1.0'
        struct.pack_into('<HBHBHHBHHHII', boot, 11, 512, self.spc, 32, 2, 0, 0,
                         0xF8, 0, 63, 255, 0, self.total_sectors)
        struct.pack_into('<IHHIHH', boot, 36, self.fat_sectors, 0, 0, 2, 1, 6)
        boot[64:67] = bytes((0x80, 0, 0x29))
        struct.pack_into('<I', boot, 67, 0x56494E59)
        boot[71:82] = b'VINYLFPGA  '
        boot[82:90] = b'FAT32   '
        boot[510:512] = b'\x55\xAA'
        self.fh.seek(0); self.fh.write(boot)
        self.fh.seek(6 * 512); self.fh.write(boot)

    def __enter__(self): return self

    def __exit__(self, *args): self.close()

    def add_file(self, name, source, *, cancel=None):
        name = name.upper()
        if not re.fullmatch(r'[A-Z0-9_]{1,8}\.[A-Z0-9_]{1,3}', name):
            raise ValueError('文件名必须为简单 ASCII 8.3 名称')
        if name in [r['name'] for r in self.records]: raise ValueError('文件名重复：' + name)
        if len(self.entries) >= 16: raise ValueError('根目录首扇区的 16 项已满')
        is_bytes = isinstance(source, (bytes, bytearray))
        size = len(source) if is_bytes else Path(source).stat().st_size
        if size < 1 or size > 0xFFFFFFFF: raise ValueError('媒体文件必须非空且小于 4 GiB')
        clusters = math.ceil(size / (self.spc * 512))
        first, end = self.next_cluster, self.next_cluster + clusters
        if end > self.cluster_count + 2: raise ValueError('镜像容量不足')
        for cluster in range(first, end):
            struct.pack_into('<I', self.fat, cluster * 4, cluster + 1 if cluster + 1 < end else 0x0FFFFFFF)
        self.next_cluster = end
        lba = self.data_lba + (first - 2) * self.spc
        self.fh.seek(lba * 512)
        digest = hashlib.sha256()
        if is_bytes:
            self.fh.write(source); digest.update(source)
        else:
            with Path(source).open('rb') as stream:
                remaining = size
                while remaining:
                    if cancel is not None and cancel.is_set(): raise ValueError('用户已取消生成')
                    data = stream.read(min(4 << 20, remaining))
                    if not data: raise ValueError('源文件在镜像生成时被截断')
                    self.fh.write(data); digest.update(data); remaining -= len(data)
        stem, ext = name.split('.')
        entry = bytearray(32)
        entry[:11] = (stem.ljust(8) + ext.ljust(3)).encode('ascii')
        entry[11] = 0x20
        struct.pack_into('<H', entry, 20, first >> 16)
        struct.pack_into('<H', entry, 26, first & 65535)
        struct.pack_into('<I', entry, 28, size)
        self.entries.append(entry)
        record = dict(name=name, cluster=first, lba=lba, bytes=size, sha256=digest.hexdigest(), clusters=clusters)
        self.records.append(record)
        return record

    def replace_file(self, name, data):
        record = next(r for r in self.records if r['name'] == name)
        if len(data) != record['bytes']: raise ValueError('只能替换相同长度文件')
        self.fh.seek(record['lba'] * 512); self.fh.write(data)
        record['sha256'] = hashlib.sha256(data).hexdigest()

    def close(self):
        if self.closed: return
        try:
            for copy in range(2):
                self.fh.seek((self.reserved + copy * self.fat_sectors) * 512)
                self.fh.write(self.fat)
            self.fh.seek(self.data_lba * 512)
            self.fh.write(b''.join(self.entries).ljust(self.spc * 512, b'\0'))
            info = bytearray(512)
            info[:4] = b'RRaA'; info[484:488] = b'rrAa'
            struct.pack_into('<II', info, 488, self.cluster_count - (self.next_cluster - 2), self.next_cluster)
            info[508:512] = b'\0\0\x55\xAA'
            for lba in (1, 7):
                self.fh.seek(lba * 512); self.fh.write(info)
            self.fh.flush()
        finally:
            self.fh.close(); self.closed = True
