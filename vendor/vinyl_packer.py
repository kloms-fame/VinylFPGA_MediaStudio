#!/usr/bin/env python3
"""S4 曲目打包工具：把卡上的封面/裸 PCM 汇总成定长 INDEX.BIN 曲目索引。

用法（简体中文说明见 doc/开发资料/S4_SD卡体系构建_交付说明_20261005.md）:

    # 按 manifest 打包（推荐）
    python tools/vinyl_packer.py pack --image card.img --manifest songs.json

    # 直接沿用现有 PIC1~4.BMP + AUDIO.PCM 布局
    python tools/vinyl_packer.py pack --image card.img --demo

    # 只读校验（不改卡）：回读 INDEX.BIN 并逐字段比对
    python tools/vinyl_packer.py verify --image card.img

    # 打印索引内容
    python tools/vinyl_packer.py info --image card.img

设计要点（与 RTL 对齐，改这里必须同步改 src/include/song_index_defs.vh）:
  * INDEX.BIN 固定 **512 字节**（正好 1 个扇区、占 1 个簇），RTL 只读第一个扇区。
  * 条目只存 first_cluster；RTL 复用 fat32_file_streamer 走 FAT 链取数，
    因此封面天然支持碎片链，工具同时把解析出的物理 LBA 运行段写进报告备查。
  * 根目录只有 **第一个扇区（16 个目录项）** 会被 FPGA 扫描，
    所有被引用的文件都必须落在根目录前 16 项内，否则工具直接报错。
  * 索引本身带 entries_crc32（CRC-32/ISO-HDLC），RTL 侧逐字节重算比对。
  * 音频走 wav_stream_player 的"逐扇区 +1"读法（S0~S3 既有行为），
    因此工具默认要求音频物理连续；--allow-fragmented-audio 仅供离线索引实验，
    正式 verify 始终拒绝碎片音频，不能将该选项用于板端交付。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"VINYLIDX"
VERSION = 1
ENTRY_SIZE = 64
HEADER_SIZE = 64
MAX_SONGS = 4
INDEX_BYTES = 512
INDEX_NAME = "INDEX   BIN"

FLAG_COVER = 1 << 0
FLAG_AUDIO = 1 << 1
FLAG_LRC = 1 << 2
FLAG_MV = 1 << 3

EOC_MIN = 0x0FFFFFF8
BAD_CLUSTER = 0x0FFFFFF7


class PackError(RuntimeError):
    """可预期的打包/校验失败，直接给用户看的信息。"""


# ---------------------------------------------------------------------------
# 目录项 / FAT32 卷
# ---------------------------------------------------------------------------
@dataclass
class DirEntry:
    raw_index: int          # 在本扇区内的目录项序号（0..15）
    lba: int
    slot_off: int           # 扇区内字节偏移
    name: str
    attr: int
    first_cluster: int
    size: int

    @property
    def has_lfn(self) -> bool:
        return self.attr == 0x0F


@dataclass
class IndexSong:
    title: str = ""
    cover_name: str = ""
    audio_name: str = ""
    lyric_name: str = ""
    mv_name: str = ""
    mv_lba: int = 0
    mv_frames: int = 0
    mv_fps: int = 10
    mv_sha256: str = ""
    flags: int = 0
    cover_cluster: int = 0
    cover_bytes: int = 0
    cover_crc32: int = 0
    audio_cluster: int = 0
    audio_bytes: int = 0
    audio_crc32: int = 0
    audio_lba: int = 0
    cover_sha256: str = ""
    audio_sha256: str = ""
    cover_runs: list = field(default_factory=list)
    audio_runs: list = field(default_factory=list)


class Fat32Image:
    """最小 FAT32 读写器：只实现本工具需要的部分。"""

    def __init__(self, path: Path, writable: bool = False):
        self.path = path
        self.mode = "r+b" if writable else "rb"
        self.fh = path.open(self.mode)
        self._parse_bpb()

    # -- 生命周期 ----------------------------------------------------------
    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 基本读写 ----------------------------------------------------------
    def read_sector(self, lba: int) -> bytes:
        self.fh.seek(lba * self.bytes_per_sector)
        data = self.fh.read(self.bytes_per_sector)
        if len(data) != self.bytes_per_sector:
            raise PackError(f"读取 LBA {lba} 失败（文件被截断？）")
        return data

    def write_sector(self, lba: int, data: bytes) -> None:
        if len(data) != self.bytes_per_sector:
            raise PackError("write_sector 只接受整扇区数据")
        if self.mode != "r+b":
            raise PackError("当前为只读模式")
        self.fh.seek(lba * self.bytes_per_sector)
        self.fh.write(data)
        try:
            self.fh.flush()
        except OSError:
            # Windows raw volume 句柄 flush 可能 EBADF，
            # 数据已进入 OS 缓存；不阻断写入流程。
            pass

    # -- BPB ---------------------------------------------------------------
    def _parse_bpb(self) -> None:
        self.bytes_per_sector = 512     # BPB 解析期间先用默认值读 LBA0
        try:
            self.fh.seek(0, 2)
            total_size = self.fh.tell()
        except OSError:
            # Windows raw volume 设备（\\.\X:）不支持 seek end，
            # 总大小延后用 BPB total_sectors 推算。
            total_size = None

        boot = self.read_sector(0)
        if boot[510:512] != b"\x55\xaa":
            raise PackError("LBA0 缺少 0x55AA 引导签名")

        partition_lba = 0
        if boot[0] not in (0xEB, 0xE9):
            ptype = boot[446 + 4]
            if ptype not in (0x0B, 0x0C):
                raise PackError(f"MBR 分区类型 0x{ptype:02X} 不是 FAT32（0x0B/0x0C）")
            partition_lba = struct.unpack_from("<I", boot, 446 + 8)[0]
            boot = self.read_sector(partition_lba)
            if boot[510:512] != b"\x55\xaa":
                raise PackError("分区引导扇区缺少 0x55AA 签名")

        (bps,) = struct.unpack_from("<H", boot, 11)
        spc = boot[13]
        (reserved,) = struct.unpack_from("<H", boot, 14)
        num_fats = boot[16]
        (root_entries,) = struct.unpack_from("<H", boot, 17)
        (fat_size16,) = struct.unpack_from("<H", boot, 22)
        (total16,) = struct.unpack_from("<H", boot, 19)
        (total32,) = struct.unpack_from("<I", boot, 32)
        (fat_size32,) = struct.unpack_from("<I", boot, 36)
        (root_cluster,) = struct.unpack_from("<I", boot, 44)

        if bps != 512:
            raise PackError(f"只支持 512 字节/扇区，当前 {bps}")
        if spc == 0 or (spc & (spc - 1)) != 0 or spc > 128:
            raise PackError(f"非法 sectors_per_cluster = {spc}")
        if reserved == 0:
            raise PackError("reserved_sectors = 0")
        if root_entries != 0:
            raise PackError("不是 FAT32（BPB_RootEntCnt != 0）")
        if fat_size16 != 0:
            raise PackError("不是 FAT32（BPB_FATSz16 != 0）")
        if num_fats not in (1, 2):
            raise PackError(f"非法 FAT 数量 {num_fats}")
        if total32 == 0 or fat_size32 == 0 or root_cluster < 2:
            raise PackError("BPB 关键字段非法（TotalSec32/FATSz32/RootClus）")

        self.bytes_per_sector = bps
        self.sectors_per_cluster = spc
        self.spc_shift = spc.bit_length() - 1
        self.reserved_sectors = reserved
        self.num_fats = num_fats
        self.total_sectors = total32 if total16 == 0 else total32
        self.fat_size = fat_size32
        self.root_cluster = root_cluster
        self.partition_lba = partition_lba
        self.fat_start_lba = partition_lba + reserved
        self.fat_span = fat_size32 * num_fats
        self.data_start_lba = self.fat_start_lba + self.fat_span
        data_sectors = self.total_sectors - reserved - self.fat_span
        self.max_cluster = (data_sectors >> self.spc_shift) + 1
        if total_size is None:
            total_size = self.total_sectors * bps
        self.image_bytes = total_size

        if self.data_start_lba * bps > total_size:
            raise PackError("镜像/介质长度小于 BPB 声明的数据区起点")

    # -- FAT ---------------------------------------------------------------
    def fat_get(self, cluster: int) -> int:
        off = cluster * 4
        lba = self.fat_start_lba + (off >> 9)
        sec = self.read_sector(lba)
        (value,) = struct.unpack_from("<I", sec, off & 0x1FF)
        return value & 0x0FFFFFFF

    def fat_set(self, cluster: int, value: int) -> None:
        off = cluster * 4
        for copy in range(self.num_fats):
            lba = self.fat_start_lba + copy * self.fat_size + (off >> 9)
            sec = bytearray(self.read_sector(lba))
            struct.pack_into("<I", sec, off & 0x1FF, value & 0x0FFFFFFF)
            self.write_sector(lba, bytes(sec))

    def cluster_lba(self, cluster: int) -> int:
        return self.data_start_lba + ((cluster - 2) << self.spc_shift)

    def iter_chain(self, first_cluster: int, limit: int = 1 << 20):
        cluster = first_cluster
        seen = set()
        while True:
            if cluster < 2 or cluster >= self.max_cluster:
                raise PackError(f"簇号 {cluster} 越界（合法 2..{self.max_cluster - 1}）")
            if cluster in seen:
                raise PackError(f"FAT 链自环于簇 {cluster}")
            seen.add(cluster)
            if len(seen) > limit:
                raise PackError("FAT 链过长（疑似损坏）")
            yield cluster
            nxt = self.fat_get(cluster)
            if nxt >= EOC_MIN:
                return
            if nxt == BAD_CLUSTER:
                raise PackError(f"簇 {cluster} 后指向坏簇标记")
            if nxt < 2:
                raise PackError(f"簇 {cluster} 后指向空闲簇 {nxt}（链提前结束）")
            cluster = nxt

    def read_file(self, first_cluster: int, size: int) -> bytes:
        out = bytearray()
        for cluster in self.iter_chain(first_cluster):
            lba = self.cluster_lba(cluster)
            for i in range(self.sectors_per_cluster):
                if len(out) >= size:
                    break
                out += self.read_sector(lba + i)
            if len(out) >= size:
                break
        return bytes(out[:size])

    def cluster_runs(self, first_cluster: int) -> list:
        """把簇链折算成物理 LBA 运行段 [(lba, sectors), ...]，便于追溯。"""
        runs = []
        for cluster in self.iter_chain(first_cluster):
            lba = self.cluster_lba(cluster)
            if runs and runs[-1][0] + runs[-1][1] == lba:
                runs[-1][1] += self.sectors_per_cluster
            else:
                runs.append([lba, self.sectors_per_cluster])
        return [(a, b) for a, b in runs]

    # -- 根目录 ------------------------------------------------------------
    @staticmethod
    def _short_name(raw: bytes) -> str:
        base = raw[0:8].decode("ascii", "replace").rstrip(" ")
        ext = raw[8:11].decode("ascii", "replace").rstrip(" ")
        return f"{base}.{ext}" if ext else base

    def root_entries(self, max_slots: int = 16):
        """按扇区顺序列举根目录项；同时给出全局序号与所属 LBA。"""
        slot = 0
        for cluster in self.iter_chain(self.root_cluster):
            lba0 = self.cluster_lba(cluster)
            for s in range(self.sectors_per_cluster):
                sec = self.read_sector(lba0 + s)
                for i in range(0, self.bytes_per_sector, 32):
                    raw = sec[i:i + 32]
                    if raw[0] == 0x00:
                        return
                    if raw[0] == 0xE5:
                        slot += 1
                        continue
                    attr = raw[11]
                    if attr == 0x0F:
                        slot += 1
                        continue
                    first = (struct.unpack_from("<H", raw, 20)[0] << 16) | \
                            struct.unpack_from("<H", raw, 26)[0]
                    size = struct.unpack_from("<I", raw, 28)[0]
                    yield DirEntry(slot, lba0 + s, i, self._short_name(raw),
                                   attr, first, size)
                    slot += 1

    def find_root_file(self, name: str, require_first_sector: bool = True) -> DirEntry:
        for e in self.root_entries():
            if e.name.upper() == name.upper():
                if require_first_sector and e.raw_index >= 16:
                    raise PackError(
                        f"{name} 落在根目录第 {e.raw_index + 1} 项，超出 FPGA 只扫描"
                        f"的首扇区（前 16 项）；请把它前移或重建卡结构")
                return e
        raise PackError(f"根目录（首簇）中找不到 {name}")

    def free_root_slot(self):
        """在根目录**首扇区**找空槽（0x00 或 0xE5），返回 (lba, off)。"""
        sec = self.read_sector(self.cluster_lba(self.root_cluster))
        for i in range(0, self.bytes_per_sector, 32):
            if sec[i] in (0x00, 0xE5):
                return self.cluster_lba(self.root_cluster), i
        raise PackError("根目录首扇区已满（16 项用尽），无法写入 INDEX.BIN 目录项")

    def alloc_cluster(self) -> int:
        for cluster in range(2, self.max_cluster):
            if self.fat_get(cluster) == 0:
                self.fat_set(cluster, 0x0FFFFFFF)
                return cluster
        raise PackError("FAT 中没有空闲簇可分配给 INDEX.BIN")

    def write_root_entry(self, lba: int, off: int, name83: bytes,
                         first_cluster: int, size: int) -> None:
        sec = bytearray(self.read_sector(lba))
        sec[off:off + 32] = b"\x00" * 32
        sec[off:off + 11] = name83
        sec[off + 11] = 0x20                       # ATTR_ARCHIVE
        sec[off + 12] = 0x00                       # NtRes
        struct.pack_into("<H", sec, off + 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", sec, off + 26, first_cluster & 0xFFFF)
        struct.pack_into("<I", sec, off + 28, size)
        self.write_sector(lba, bytes(sec))


# ---------------------------------------------------------------------------
# INDEX.BIN 编解码（与 RTL 语义一致）
# ---------------------------------------------------------------------------
def build_entry(song: IndexSong) -> bytes:
    buf = bytearray(ENTRY_SIZE)
    struct.pack_into("<I", buf, 0, song.flags & 0xF)
    struct.pack_into("<I", buf, 4, song.cover_cluster)
    struct.pack_into("<I", buf, 8, song.cover_bytes)
    struct.pack_into("<I", buf, 12, song.cover_crc32)
    struct.pack_into("<I", buf, 16, song.audio_cluster)
    struct.pack_into("<I", buf, 20, song.audio_bytes)
    struct.pack_into("<I", buf, 24, song.audio_crc32)
    struct.pack_into("<I", buf, 28, song.audio_lba)
    title = song.title.encode("ascii", "replace")[:23]
    buf[32:32 + len(title)] = title
    if song.flags & FLAG_MV:
        if not (0 < song.mv_frames <= 65535 and song.mv_fps == 10 and
                0 < song.mv_lba <= 0xFFFFFFFF - song.mv_frames * 300):
            raise PackError("MV requires 1..65535 frames, 10 fps and non-overflowing LBA")
        struct.pack_into("<IHBB", buf, 56, song.mv_lba, song.mv_frames, song.mv_fps, 1)
    return bytes(buf)


def build_index(songs: list) -> bytes:
    if not 1 <= len(songs) <= MAX_SONGS:
        raise PackError(f"曲目数量必须在 1..{MAX_SONGS}，当前 {len(songs)}")
    payload = b"".join(build_entry(s) for s in songs)
    entries_crc = zlib.crc32(payload) & 0xFFFFFFFF

    header = bytearray(HEADER_SIZE)
    header[0:8] = MAGIC
    struct.pack_into("<H", header, 8, 2 if any(s.flags & FLAG_MV for s in songs) else VERSION)
    struct.pack_into("<H", header, 10, ENTRY_SIZE)
    struct.pack_into("<H", header, 12, len(songs))
    struct.pack_into("<H", header, 14, MAX_SONGS)
    struct.pack_into("<I", header, 16, entries_crc)
    struct.pack_into("<I", header, 20, 0x1)        # bit0: 由 vinyl_packer 生成
    image = bytes(header) + payload
    return image + bytes(INDEX_BYTES - len(image))


def parse_index(data: bytes) -> dict:
    """按 RTL 同样的顺序校验并解析；返回 dict 或抛 PackError。"""
    if len(data) < INDEX_BYTES:
        raise PackError(f"INDEX.BIN 不足 {INDEX_BYTES} 字节（实际 {len(data)}）")
    if data[0:8] != MAGIC:
        raise PackError("索引 magic 不匹配（缺少 VINYLIDX）")
    version, entry_size, count, max_songs = struct.unpack_from("<HHHH", data, 8)
    if version not in (1, 2):
        raise PackError(f"索引版本 {version} != {VERSION}")
    if entry_size != ENTRY_SIZE:
        raise PackError(f"条目长度 {entry_size} != {ENTRY_SIZE}")
    if not 1 <= count <= MAX_SONGS:
        raise PackError(f"曲目数量 {count} 超出 1..{MAX_SONGS}")
    (entries_crc,) = struct.unpack_from("<I", data, 16)
    payload = data[HEADER_SIZE:HEADER_SIZE + count * ENTRY_SIZE]
    calc = zlib.crc32(payload) & 0xFFFFFFFF
    if calc != entries_crc:
        raise PackError(f"entries_crc32 不匹配（文件 0x{entries_crc:08X} / 实算 0x{calc:08X}）")

    songs = []
    for i in range(count):
        o = HEADER_SIZE + i * ENTRY_SIZE
        flags, cov_cl, cov_by, cov_crc, aud_cl, aud_by, aud_crc, aud_lba = \
            struct.unpack_from("<IIIIIIII", data, o)
        title = data[o + 32:o + 56].split(b"\x00", 1)[0].decode("ascii", "replace")
        mv_lba, mv_frames, mv_fps, mv_codec = struct.unpack_from("<IHBB", data, o + 56)
        if version == 2 and flags & FLAG_MV:
            if not (mv_lba and mv_frames and mv_fps == 10 and mv_codec == 1 and
                    mv_lba + mv_frames * 300 <= 0xFFFFFFFF):
                raise PackError(f"song {i}: invalid MV metadata")
        else:
            mv_lba = mv_frames = mv_fps = mv_codec = 0
        songs.append({"flags": flags, "cover_cluster": cov_cl, "cover_bytes": cov_by,
                      "cover_crc32": cov_crc, "audio_cluster": aud_cl,
                      "audio_bytes": aud_by, "audio_crc32": aud_crc,
                      "audio_lba": aud_lba, "title": title,
                      "mv_lba": mv_lba, "mv_frames": mv_frames,
                      "mv_fps": mv_fps, "mv_codec": mv_codec})
    return {"version": version, "entry_size": entry_size, "count": count,
            "max_songs": max_songs, "entries_crc32": entries_crc, "songs": songs}


# ---------------------------------------------------------------------------
# 打包流程
# ---------------------------------------------------------------------------
BMP_HEADER_MIN = 54


def _check_cover(data: bytes, name: str, width: int, height: int) -> None:
    if len(data) < BMP_HEADER_MIN:
        raise PackError(f"{name}: 不足 54 字节 BMP 头")
    if data[0:2] != b"BM":
        raise PackError(f"{name}: 不是 BMP（缺少 'BM'）")
    file_len = struct.unpack_from("<I", data, 2)[0]
    data_off = struct.unpack_from("<I", data, 10)[0]
    dib = struct.unpack_from("<I", data, 14)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    planes, bpp = struct.unpack_from("<HH", data, 26)
    comp = struct.unpack_from("<I", data, 30)[0]
    need = data_off + width * height * 3
    ok = (dib >= 40 and w == width and h == height and planes == 1 and bpp == 24
          and comp == 0 and data_off == BMP_HEADER_MIN and data_off < file_len
          and file_len >= need and len(data) >= need)
    if not ok:
        raise PackError(
            f"{name}: 不符合 RTL 的封面约束（需 {width}x{height} 24bit 未压缩 BMP、像素偏移 54，"
            f"实测 {w}x{h} {bpp}bpp comp={comp} len={file_len} off={data_off}）")


def _load_songs_from_manifest(path: Path) -> list:
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw = doc.get("songs")
    if not isinstance(raw, list) or not raw:
        raise PackError("manifest 必须包含非空 songs 数组")
    songs = []
    for i, item in enumerate(raw):
        cover = item.get("cover")
        audio = item.get("audio")
        if not cover:
            raise PackError(f"songs[{i}] 缺少 cover")
        songs.append(IndexSong(title=item.get("title", ""), cover_name=cover,
                               audio_name=audio or "",
                               lyric_name=item.get("lyric", ""),
                               mv_name=item.get("mv", "")))
    return songs


def _load_songs_demo() -> list:
    songs = []
    for i in range(1, 5):
        songs.append(IndexSong(title=f"Track {i}", cover_name=f"PIC{i}.BMP",
                               audio_name="AUDIO.PCM" if i == 1 else ""))
    return songs


def cmd_pack(args) -> int:
    image_path = Path(args.image)
    width, height = args.width, args.height
    songs = _load_songs_demo() if args.demo else _load_songs_from_manifest(Path(args.manifest))
    if len(songs) > MAX_SONGS:
        raise PackError(f"曲目数量 {len(songs)} 超过上限 {MAX_SONGS}（索引超限必须拒绝，不得静默越界）")

    with Fat32Image(image_path, writable=True) as img:
        # 1) 定位并解析每个引用的文件
        for i, s in enumerate(songs):
            cover = img.find_root_file(s.cover_name)
            if cover.size == 0 or cover.first_cluster < 2:
                raise PackError(f"{s.cover_name}: 目录项簇号/长度非法")
            cdata = img.read_file(cover.first_cluster, cover.size)
            _check_cover(cdata, s.cover_name, width, height)
            s.flags |= FLAG_COVER
            s.cover_cluster = cover.first_cluster
            s.cover_bytes = cover.size
            s.cover_crc32 = zlib.crc32(cdata) & 0xFFFFFFFF
            s.cover_sha256 = hashlib.sha256(cdata).hexdigest()
            s.cover_runs = img.cluster_runs(cover.first_cluster)

            if s.audio_name:
                audio = img.find_root_file(s.audio_name)
                if audio.size == 0 or audio.first_cluster < 2:
                    raise PackError(f"{s.audio_name}: 目录项簇号/长度非法")
                if audio.size & 1:
                    raise PackError(
                        f"{s.audio_name}: 音频字节数 {audio.size} 为奇数；"
                        "16 bit 单声道 PCM 必须按偶数字节存放")
                adata = img.read_file(audio.first_cluster, audio.size)
                s.flags |= FLAG_AUDIO
                s.audio_cluster = audio.first_cluster
                s.audio_bytes = audio.size
                s.audio_crc32 = zlib.crc32(adata) & 0xFFFFFFFF
                s.audio_sha256 = hashlib.sha256(adata).hexdigest()
                s.audio_runs = img.cluster_runs(audio.first_cluster)
                if len(s.audio_runs) != 1 and not args.allow_fragmented_audio:
                    raise PackError(
                        f"{s.audio_name}: 音频在物理上不连续（{len(s.audio_runs)} 段）。"
                        "wav_stream_player 逐扇区 +1 读取，必须连续；"
                        "如需先出索引，可加 --allow-fragmented-audio 并记录风险")
                s.audio_lba = s.audio_runs[0][0]

            if s.mv_name:
                mv = img.find_root_file(s.mv_name)
                if not mv.size or mv.size % 153600:
                    raise PackError(f"{s.mv_name}: MV must contain whole 320x240 RGB565 frames")
                runs = img.cluster_runs(mv.first_cluster)
                if len(runs) != 1:
                    raise PackError(f"{s.mv_name}: fragmented MV is not supported; copy again contiguously")
                s.mv_lba, s.mv_frames = runs[0][0], mv.size // 153600
                s.mv_sha256 = hashlib.sha256(img.read_file(mv.first_cluster, mv.size)).hexdigest()
                s.flags |= FLAG_MV

        blob = build_index(songs)
        parsed = parse_index(blob)          # 自检：写卡前先过一遍 RTL 同款校验

        # 2) 写入 INDEX.BIN（复用已有簇或分配新簇 + 根目录首扇区目录项）
        try:
            existing = img.find_root_file("INDEX.BIN", require_first_sector=False)
        except PackError:
            existing = None
        if existing and existing.first_cluster >= 2:
            index_cluster = existing.first_cluster
            chain = list(img.iter_chain(index_cluster))
            for extra in chain[1:]:         # 旧索引若占多个簇，释放多余簇
                img.fat_set(extra, 0)
        else:
            index_cluster = img.alloc_cluster()
            lba, off = img.free_root_slot()
            img.write_root_entry(lba, off, INDEX_NAME.encode("ascii"),
                                 index_cluster, INDEX_BYTES)

        index_lba = img.cluster_lba(index_cluster)
        img.write_sector(index_lba, blob[:512])
        for i in range(1, img.sectors_per_cluster):   # 清掉该簇剩余扇区
            img.write_sector(index_lba + i, bytes(512))

        # 3) PC 侧扇区回读校验（用同一套解析逻辑再读一遍）
        readback = img.read_sector(index_lba)
        reparsed = parse_index(readback)
        if reparsed["songs"] != parsed["songs"]:
            raise PackError("回读校验失败：重读 INDEX.BIN 与写入内容不一致")

        report = {
            "index_file": "INDEX.BIN",
            "index_cluster": index_cluster,
            "index_lba": index_lba,
            "index_bytes": INDEX_BYTES,
            "entries_crc32": f"0x{parsed['entries_crc32']:08X}",
            "song_count": parsed["count"],
            "max_songs": MAX_SONGS,
            "volume": {
                "bytes_per_sector": img.bytes_per_sector,
                "sectors_per_cluster": img.sectors_per_cluster,
                "fat_start_lba": img.fat_start_lba,
                "data_start_lba": img.data_start_lba,
                "max_cluster": img.max_cluster,
            },
            "songs": [],
        }
        for i, s in enumerate(songs):
            report["songs"].append({
                "index": i,
                "title": s.title,
                "mv": ({"name": s.mv_name, "start_lba": s.mv_lba,
                        "frames": s.mv_frames, "fps": s.mv_fps,
                        "bytes": s.mv_frames * 153600, "sha256": s.mv_sha256,
                        "format": "RGB565LE 320x240 top-down row-major"}
                       if s.mv_name else None),
                "cover": {"name": s.cover_name, "first_cluster": s.cover_cluster,
                          "bytes": s.cover_bytes,
                          "sectors": (s.cover_bytes + 511) // 512,
                          "lba_runs": s.cover_runs,
                          "crc32": f"0x{s.cover_crc32:08X}",
                          "sha256": s.cover_sha256},
                "audio": ({"name": s.audio_name, "first_cluster": s.audio_cluster,
                           "bytes": s.audio_bytes,
                           "sectors": (s.audio_bytes + 511) // 512,
                           "lba_runs": s.audio_runs, "start_lba": s.audio_lba,
                           "crc32": f"0x{s.audio_crc32:08X}",
                           "sha256": s.audio_sha256}
                          if s.audio_name else None),
            })

    report_path = Path(args.report) if args.report else image_path.with_name("INDEX.json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"PACK_OK INDEX.BIN 已写入 {image_path}（簇 {index_cluster} / LBA {index_lba}）")
    print(f"PACK_OK 曲目 {parsed['count']}/{MAX_SONGS}，entries_crc32 {report['entries_crc32']}")
    print(f"PACK_OK 回读校验通过，报告 {report_path}")
    return 0


def cmd_info(args) -> int:
    with Fat32Image(Path(args.image)) as img:
        e = img.find_root_file("INDEX.BIN")
        data = img.read_file(e.first_cluster, max(e.size, INDEX_BYTES))
        info = parse_index(data)
    print(f"INDEX.BIN 簇={e.first_cluster} LBA={img.cluster_lba(e.first_cluster)} "
          f"字节={e.size} 曲目={info['count']}/{info['max_songs']} "
          f"entries_crc32=0x{info['entries_crc32']:08X}")
    for i, s in enumerate(info["songs"]):
        print(f"  [{i}] flags=0x{s['flags']:X} cover_clu={s['cover_cluster']} "
              f"cover_bytes={s['cover_bytes']} audio_clu={s['audio_cluster']} "
              f"audio_bytes={s['audio_bytes']} title={s['title']!r} "
              f"mv_lba={s['mv_lba']} mv_frames={s['mv_frames']} mv_fps={s['mv_fps']}")
    return 0


def cmd_verify(args) -> int:
    image_path = Path(args.image)
    with Fat32Image(image_path) as img:
        e = img.find_root_file("INDEX.BIN")
        data = img.read_file(e.first_cluster, max(e.size, INDEX_BYTES))
        info = parse_index(data)
        width, height = args.width, args.height
        print(f"VERIFY_OK 索引可解析：曲目 {info['count']}，"
              f"entries_crc32=0x{info['entries_crc32']:08X}")
        for i, s in enumerate(info["songs"]):
            if s["mv_frames"]:
                matches = [e for e in img.root_entries() if e.first_cluster >= 2 and
                           img.cluster_lba(e.first_cluster) == s["mv_lba"]]
                if len(matches) != 1 or matches[0].size != s["mv_frames"] * 153600:
                    raise PackError(f"songs[{i}].mv LBA/length does not match a root file")
                mv = matches[0]
                if len(img.cluster_runs(mv.first_cluster)) != 1:
                    raise PackError(f"songs[{i}].mv fragmented")
                digest = hashlib.sha256(img.read_file(mv.first_cluster,mv.size)).hexdigest()
                print(f"VERIFY_OK 曲目 {i}: MV {mv.name} frames={s['mv_frames']} "
                      f"fps=10 SHA256={digest}")
            cov = img.read_file(s["cover_cluster"], s["cover_bytes"])
            _check_cover(cov, f"songs[{i}].cover", width, height)
            got = zlib.crc32(cov) & 0xFFFFFFFF
            if got != s["cover_crc32"]:
                raise PackError(f"songs[{i}].cover CRC 不符：卡上 0x{got:08X} != "
                                f"索引 0x{s['cover_crc32']:08X}")
            runs = img.cluster_runs(s["cover_cluster"])
            print(f"VERIFY_OK 曲目 {i}: 封面 簇{s['cover_cluster']} "
                  f"{s['cover_bytes']}B 运行段 {runs} CRC32 一致")
            if s["flags"] & FLAG_AUDIO:
                aruns = img.cluster_runs(s["audio_cluster"])
                if len(aruns) != 1:
                    raise PackError(f"songs[{i}].audio fragmented: FPGA requires contiguous sectors; "
                                    "copy the PCM file again contiguously")
                aud = img.read_file(s["audio_cluster"], s["audio_bytes"])
                got = zlib.crc32(aud) & 0xFFFFFFFF
                if got != s["audio_crc32"]:
                    raise PackError(f"songs[{i}].audio CRC 不符：卡上 0x{got:08X} != "
                                    f"索引 0x{s['audio_crc32']:08X}")
                print(f"VERIFY_OK 曲目 {i}: 音频 簇{s['audio_cluster']} "
                      f"{s['audio_bytes']}B 起始LBA {aruns[0][0]} 运行段 {aruns} CRC32 一致")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="VinylFPGA S4 曲目索引打包/校验工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = dict()
    pp = sub.add_parser("pack", help="写入/更新 INDEX.BIN 并输出报告")
    pp.add_argument("--image", required=True, help="FAT32 镜像或整盘设备")
    src = pp.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="JSON manifest: {songs:[{title,cover,audio}]}")
    src.add_argument("--demo", action="store_true", help="使用 PIC1~4.BMP + AUDIO.PCM")
    pp.add_argument("--report", help="报告输出路径（默认与镜像同目录 INDEX.json）")
    pp.add_argument("--width", type=int, default=640)
    pp.add_argument("--height", type=int, default=480)
    pp.add_argument("--allow-fragmented-audio", action="store_true",
                    help="仅允许离线生成碎片音频索引；正式 verify 仍拒绝，不能用于板端交付")
    pp.set_defaults(func=cmd_pack)

    pi = sub.add_parser("info", help="打印 INDEX.BIN 内容")
    pi.add_argument("--image", required=True)
    pi.set_defaults(func=cmd_info)

    pv = sub.add_parser("verify", help="只读校验索引、文件 CRC 及板端所需音频连续布局")
    pv.add_argument("--image", required=True)
    pv.add_argument("--width", type=int, default=640)
    pv.add_argument("--height", type=int, default=480)
    pv.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except PackError as exc:
        print(f"PACK_FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
