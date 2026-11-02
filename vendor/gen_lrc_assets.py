#!/usr/bin/env python3
"""S5 歌词资源生成器：把 .lrc 变成 FPGA 可用的字库 ROM + 歌词行表 ROM。

    python tools/gen_lrc_assets.py --lrc doc/资源/lyrics/demo.lrc --verify

产物（都是可综合的 Verilog ROM，综合时进入 BRAM）：
  * src/display/lrc_font_rom.v  —— 16x16 点阵字库，addr = 字形号*16 + 行号
  * src/display/lrc_lines_rom.v —— 每行：起始毫秒 + 若干字形号
  * src/include/lrc_geom.vh     —— 行数/每行字数/字号/位置/颜色等几何宏
  * doc/资源/lyrics/demo_preview.png（--verify：还原预览，人工核对字形）

设计取舍（对应报告 S5"字库容量必须先做 BRAM 预算"）：
  * 只收录**该歌词文件真正用到的字符**（外加空格），而不是整张字库，
    60 个字形的 16x16 点阵约 2KB，落 2~3 个 BRAM9K。
  * 字形统一 16x16、行表定长，渲染只需"行表查一次 + 字库查一次"，路径短。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]

GLYPH_W = 16
GLYPH_H = 16
CHARS_PER_LINE = 16
OSD_X = 96
OSD_Y = 340        # 落在画面中下部，避开底部波形条（Y 376..471）
SCALE = 1
COLOR = (0xFF, 0xE8, 0x60)      # 暖金色，与开机 Logo 一致
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


class LrcError(RuntimeError):
    pass


def parse_lrc(path: Path) -> list:
    """解析 [mm:ss.xx] 行，返回 [(time_ms, text)]，按时间排序。"""
    tag = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        stamps = list(tag.finditer(raw))
        if not stamps:
            continue
        text = tag.sub("", raw).strip()
        if not text:
            continue
        for m in stamps:
            mm, ss = int(m.group(1)), float(m.group(2))
            lines.append((int(round((mm * 60 + ss) * 1000)), text))
    if not lines:
        raise LrcError(f"{path} 中没有解析到任何 [mm:ss.xx] 歌词行")
    lines.sort(key=lambda x: x[0])
    if len(lines) > 15:
        raise LrcError(f"歌词行数 {len(lines)} 超过行表上限 15（S5 先支持有限行表）")
    for _, text in lines:
        if len(text) > CHARS_PER_LINE:
            raise LrcError(f"单行 {len(text)} 字超过每行上限 {CHARS_PER_LINE}: {text!r}")
    return lines


def build_charset(lines: list) -> list:
    chars = sorted({ch for _, text in lines for ch in text})
    if " " not in chars:
        chars.append(" ")
    chars = sorted(chars)
    if len(chars) > 240:
        raise LrcError(f"字形数 {len(chars)} 过多，BRAM 预算不允许")
    return chars


def find_font() -> Path:
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            return Path(cand)
    raise LrcError("找不到可用的中文字体（msyh/simhei/simsun）")


def render_glyph(ch, is_ascii: bool) -> list:
    """渲染成 16x16 位图（每行 16bit，MSB 在左）。

    关键点：不能直接把字形画在 (0,0) —— 中文字号 16 的包围盒常略大于 16x16，
    会被裁掉笔画。这里按包围盒自适应缩小并居中，保证整字完整落在格子里。
    """
    font_path = str(find_font())
    size = 12 if is_ascii else GLYPH_H
    img = Image.new("1", (GLYPH_W, GLYPH_H), 0)
    for _ in range(8):
        f = ImageFont.truetype(font_path, size)
        probe = ImageDraw.Draw(Image.new("1", (GLYPH_W, GLYPH_H), 0))
        bbox = probe.textbbox((0, 0), ch, font=f)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= GLYPH_W and h <= GLYPH_H:
            break
        size -= 1
    img = Image.new("1", (GLYPH_W, GLYPH_H), 0)
    draw = ImageDraw.Draw(img)
    draw.text(((GLYPH_W - w) // 2 - bbox[0], (GLYPH_H - h) // 2 - bbox[1]),
              ch, font=f, fill=1)
    rows = []
    for y in range(GLYPH_H):
        bits = 0
        for x in range(GLYPH_W):
            if img.getpixel((x, y)):
                bits |= 1 << (GLYPH_W - 1 - x)
        rows.append(bits)
    return rows


def emit_font_rom(chars: list, glyphs: list,
                  module_name: str = "lrc_font_rom",
                  guard_name: str = "EXP_LRC_FONT_ROM_V") -> str:
    total = len(chars) * GLYPH_H
    addr_w = max(1, (total - 1).bit_length())
    depth = 1 << addr_w
    lines = [
        "// 由 tools/gen_lrc_assets.py 自动生成，请勿手改。",
        f"`ifndef {guard_name}",
        f"`define {guard_name}",
        f"module {module_name} (",
        "    input  wire clk,",
        f"    input  wire [{addr_w - 1}:0] addr,",
        "    output reg  [15:0] dout = 16'h0000",
        ");",
        "    // Single synchronous read: one clock latency, no reset on BRAM data.",
        f"    reg [15:0] row_mem [0:{depth - 1}];",
        "    initial begin",
    ]
    for gi, rows in enumerate(glyphs):
        for r, bits in enumerate(rows):
            lines.append(f"        row_mem[{gi * GLYPH_H + r}] = 16'h{bits:04X};")
    for addr in range(total, depth):
        lines.append(f"        row_mem[{addr}] = 16'h0000;")
    lines += ["    end", "    always @(posedge clk) begin",
              "        dout <= row_mem[addr];", "    end", "endmodule", "`endif", ""]
    return "\n".join(lines)


def parse_lrc_multisong(path: Path) -> list:
    """Parse a D9 LRC file with the frozen 128-line/16-glyph limits.

    Duplicate timestamps deliberately use the last source row. This prevents
    an earlier row with the same time from becoming permanently invisible.
    """
    tag = re.compile(r"\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\]")
    by_time = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        stamps = list(tag.finditer(raw))
        if not stamps:
            continue
        text = tag.sub("", raw).strip()
        if not text:
            continue
        for match in stamps:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            if seconds >= 60:
                raise LrcError(f"秒字段必须小于 60: {raw!r}")
            fraction = (match.group(3) or "").ljust(3, "0")
            time_ms = (minutes * 60 + seconds) * 1000 + int(fraction or "0")
            if time_ms > 0xFFFFFFFF:
                raise LrcError(f"时间超出 u32 毫秒范围: {raw!r}")
            by_time[time_ms] = text

    lines = sorted(by_time.items())
    if len(lines) > 128:
        raise LrcError(f"{path} 有 {len(lines)} 行，超过每曲 128 行上限")
    for time_ms, text in lines:
        if len(text) > 16:
            raise LrcError(f"时间 {time_ms} ms 的行有 {len(text)} 字，超过 16 字上限: {text!r}")
    return lines


def pack_glyph_vector(text: str, char_index: dict) -> int:
    packed = 0
    for index, char in enumerate(text):
        packed |= char_index[char] << (index * 8)
    return packed


def emit_song_text_rom(tracks: list, chars: list) -> str:
    flattened = []
    offsets = []
    for track in tracks:
        offsets.append(len(flattened))
        char_index = track["char_index"]
        for time_ms, text in track["lines"]:
            flattened.append((time_ms, len(text), pack_glyph_vector(text, char_index)))

    depth = max(1, len(flattened))
    addr_width = max(1, (depth - 1).bit_length())
    rom_depth = 1 << addr_width
    records = [0] * rom_depth
    for index, (time_ms, length, glyphs) in enumerate(flattened):
        if time_ms >= (1 << 18):
            raise LrcError(f"歌词时间 {time_ms} ms 超过 18 位 ROM 时间范围")
        records[index] = (time_ms << 133) | (length << 128) | glyphs

    glyph_chunks = [[(record >> (chunk * 32)) & 0xFFFFFFFF
                     for record in records] for chunk in range(4)]
    time_lengths = [((record >> 133) << 5) | ((record >> 128) & 0x1F)
                    for record in records]

    out = [
        "// 由 tools/gen_lrc_assets.py --songs-manifest 自动生成，请勿手改。",
        "`ifndef EXP_SONG_TEXT_ROM_V",
        "`define EXP_SONG_TEXT_ROM_V",
        "module song_text_rom (",
        "    input  wire        clk,",
        "    input  wire [1:0]  song_id,",
        "    input  wire [6:0]  line_index,",
        "    output reg  [7:0]  line_count,",
        "    output reg         line_valid,",
        "    output wire [31:0] line_time_ms,",
        "    output wire [4:0]  line_length,",
        "    output wire [127:0] line_glyphs,",
        "    output reg         song_text_valid,",
        "    output reg  [4:0]  title_length,",
        "    output reg  [4:0]  artist_length,",
        "    output reg  [127:0] title_glyphs,",
        "    output reg  [127:0] artist_glyphs,",
        "    output wire [127:0] no_lyrics_glyphs,",
        "    input  wire [7:0]  ui_ascii_code,",
        "    output reg  [7:0]  ui_glyph",
        ");",
        f"    localparam integer SONG_LINE_ADDR_W = {addr_width};",
        # Keep each synchronous ROM within a 32-bit port and power-of-two
        # depth so TD maps the lyric table into BRAM rather than LUT fabric.
        *[f"    reg [31:0] glyph_mem_{chunk} [0:{rom_depth - 1}];"
          for chunk in range(4)],
        f"    reg [22:0] time_length_mem [0:{rom_depth - 1}];",
        "    initial begin",
    ]
    for chunk, values in enumerate(glyph_chunks):
        for index, value in enumerate(values):
            out.append(f"        glyph_mem_{chunk}[{index}] = 32'h{value:08X};")
    for index, value in enumerate(time_lengths):
        out.append(f"        time_length_mem[{index}] = 23'h{value:06X};")
    out += [
        "    end",
        "    reg [SONG_LINE_ADDR_W-1:0] line_base;",
        "    wire [SONG_LINE_ADDR_W-1:0] line_addr = line_base + line_index;",
        "    reg [31:0] line_glyph_word_0, line_glyph_word_1, line_glyph_word_2, line_glyph_word_3;",
        "    reg [22:0] line_time_length_word;",
        "    assign line_time_ms = line_valid ? {14'd0, line_time_length_word[22:5]} : 32'hFFFFFFFF;",
        "    assign line_length = line_valid ? line_time_length_word[4:0] : 5'd0;",
        "    assign line_glyphs = line_valid ? {line_glyph_word_3, line_glyph_word_2, line_glyph_word_1, line_glyph_word_0} : 128'd0;",
        f"    assign no_lyrics_glyphs = 128'h{pack_glyph_vector('NO LYRICS', tracks[0]['char_index']):032X};",
        "    always @* begin",
        "        line_count = 8'd0;",
        "        line_base = {SONG_LINE_ADDR_W{1'b0}};",
        "        song_text_valid = 1'b0;",
        "        title_length = 5'd0;",
        "        artist_length = 5'd0;",
        "        title_glyphs = 128'd0;",
        "        artist_glyphs = 128'd0;",
        "        case (song_id)",
    ]
    for song_id, track in enumerate(tracks):
        char_index = track["char_index"]
        title = track["title"]
        artist = track["artist"]
        title_bus = pack_glyph_vector(title, char_index)
        artist_bus = pack_glyph_vector(artist, char_index)
        count = len(track["lines"])
        out += [
            f"            2'd{song_id}: begin",
            f"                line_count = 8'd{count};",
            f"                line_base = {addr_width}'d{offsets[song_id]};",
            f"                title_length = 5'd{len(title)};",
            f"                artist_length = 5'd{len(artist)};",
            f"                title_glyphs = 128'h{title_bus:032X};",
            f"                artist_glyphs = 128'h{artist_bus:032X};",
            f"                song_text_valid = 1'b{int(bool(title or artist or count))};",
            "            end",
        ]
    out += [
        "            default: ;",
        "        endcase",
        "    end",
        "    always @(posedge clk) begin",
        "        line_glyph_word_0 <= glyph_mem_0[line_addr];",
        "        line_glyph_word_1 <= glyph_mem_1[line_addr];",
        "        line_glyph_word_2 <= glyph_mem_2[line_addr];",
        "        line_glyph_word_3 <= glyph_mem_3[line_addr];",
        "        line_time_length_word <= time_length_mem[line_addr];",
        f"        line_valid <= (line_index < line_count) && (line_addr < {addr_width}'d{depth});",
        "    end",
        "    always @* begin",
        "        ui_glyph = 8'd0;",
        "        case (ui_ascii_code)",
    ]
    ui_char_index = tracks[0]["char_index"]
    for char in chars:
        if len(char) == 1 and ord(char) < 128 and char in ui_char_index:
            out.append(f"            8'd{ord(char)}: ui_glyph = 8'd{ui_char_index[char]};")
    out += [
        "            default: ui_glyph = 8'd0;",
        "        endcase",
        "    end",
        "endmodule",
        "`endif",
        "",
    ]
    return "\n".join(out)


def generate_song_text_assets(manifest_path: Path, out_dir: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_tracks = manifest.get("tracks")
    if not isinstance(raw_tracks, list) or len(raw_tracks) != 4:
        raise LrcError("songs manifest 必须显式提供 song_id 0..3 共四项")

    by_id = {}
    for item in raw_tracks:
        if not isinstance(item, dict):
            raise LrcError("tracks 中每项必须是对象")
        song_id = item.get("song_id")
        if not isinstance(song_id, int) or song_id < 0 or song_id > 3 or song_id in by_id:
            raise LrcError(f"song_id 必须是唯一的 0..3: {song_id!r}")
        title = item.get("title", "")
        display_title = item.get("display_title", title)
        artist = item.get("artist", "")
        if not isinstance(title, str) or not isinstance(display_title, str) or not isinstance(artist, str):
            raise LrcError(f"song_id {song_id} 的 title/artist 必须是 UTF-8 字符串")
        if len(display_title) > 16 or len(artist) > 16:
            raise LrcError(f"song_id {song_id} 的 title/artist 均最多 16 个字形")
        lrc_value = item.get("lrc")
        if lrc_value is None or lrc_value == "":
            lines = []
        elif isinstance(lrc_value, str):
            lrc_path = (manifest_path.parent / lrc_value).resolve()
            if not lrc_path.is_file():
                raise LrcError(f"song_id {song_id} 的 LRC 文件不存在: {lrc_path}")
            lines = parse_lrc_multisong(lrc_path)
        else:
            raise LrcError(f"song_id {song_id} 的 lrc 必须是相对路径或 null")
        by_id[song_id] = {"song_id": song_id, "title": display_title,
                          "artist": artist, "lines": lines}
    if set(by_id) != {0, 1, 2, 3}:
        raise LrcError("songs manifest 必须且只能包含 song_id 0..3")

    tracks = [by_id[index] for index in range(4)]
    common = {" ", "-", ":", "/"}
    common.update("TRACK 0123456789ARTISTNO LYRICS")
    banked = bool(manifest.get("glyph_banks", False))
    bank_chars = []
    if banked:
        for bank in range(2):
            bank_set = set(common)
            for song_id in range(bank * 2, bank * 2 + 2):
                track = tracks[song_id]
                bank_set.update(track["title"])
                bank_set.update(track["artist"])
                for _, text in track["lines"]:
                    bank_set.update(text)
            ordered = sorted(bank_set)
            if len(ordered) > 256:
                raise LrcError(f"字库组 {bank} 去重字形 {len(ordered)} 超过 8 bit 的 256 上限")
            bank_chars.append(ordered)
        for track in tracks:
            track["char_index"] = {
                char: index for index, char in enumerate(bank_chars[track["song_id"] // 2])}
        # The high address bit selects one of two fixed 256-glyph banks.
        font_chars = (bank_chars[0] + [" "] * (256 - len(bank_chars[0])) +
                      bank_chars[1] + [" "] * (256 - len(bank_chars[1])))
        chars = sorted(set().union(*map(set, bank_chars)))
        font_depth, font_addr_width = 8192, 13
    else:
        chars_set = set(common)
        for track in tracks:
            chars_set.update(track["title"])
            chars_set.update(track["artist"])
            for _, text in track["lines"]:
                chars_set.update(text)
        chars = sorted(chars_set)
        if len(chars) > 256:
            raise LrcError(f"去重字形 {len(chars)} 超过 8 bit glyph_id 可表示的 256")
        for track in tracks:
            track["char_index"] = {char: index for index, char in enumerate(chars)}
        font_chars = chars
        font_addr_width = max(1, (len(chars) * GLYPH_H - 1).bit_length())
        font_depth = 1 << font_addr_width
        bank_chars = [chars]
    glyphs = [render_glyph(char, ord(char) < 128) for char in font_chars]

    display_dir = out_dir / "src/display"
    include_dir = out_dir / "src/include"
    display_dir.mkdir(parents=True, exist_ok=True)
    include_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "src/display/song_text_rom.v": emit_song_text_rom(tracks, chars),
        "src/display/song_font_rom.v": emit_font_rom(
            font_chars, glyphs, "song_font_rom", "EXP_SONG_FONT_ROM_V"),
        "src/include/song_text_geom.vh": "\n".join([
            "// 由 tools/gen_lrc_assets.py --songs-manifest 自动生成，请勿手改。",
            f"localparam integer SONG_TEXT_LINE_TOTAL = {sum(len(t['lines']) for t in tracks)};",
            f"localparam integer SONG_TEXT_GLYPH_COUNT = {sum(map(len, bank_chars))};",
            f"localparam integer SONG_FONT_ADDR_W = {font_addr_width};",
            f"localparam integer SONG_FONT_DEPTH = {font_depth};",
            "localparam integer SONG_TEXT_MAX_LINES = 128;",
            "localparam integer SONG_TEXT_MAX_CHARS = 16;",
            "",
        ]),
    }
    for relative, content in files.items():
        (out_dir / relative).write_text(content, encoding="utf-8", newline="\n")
    return {"tracks": tracks, "chars": chars, "bank_chars": bank_chars,
            "font_addr_width": font_addr_width, "font_depth": font_depth}


def emit_lines_rom(lines_data: list) -> str:
    out = [
        "// 由 tools/gen_lrc_assets.py 自动生成，请勿手改。",
        "`ifndef EXP_LRC_LINES_ROM_V",
        "`define EXP_LRC_LINES_ROM_V",
        "module lrc_lines_rom (",
        "    input  wire [3:0]  line,",
        "    input  wire [3:0]  col,",
        "    output reg  [31:0] time_ms,",
        "    output reg  [7:0]  glyph",
        ");",
        "    always @* begin",
        "        time_ms = 32'd0;",
        "        glyph   = 8'd0;",
        "        case (line)",
    ]
    for idx, (t_ms, glyph_idx, used) in enumerate(lines_data):
        out.append(f"            4'd{idx}: begin")
        out.append(f"                time_ms = 32'd{t_ms};")
        out.append(f"                case (col)")
        for c, g in enumerate(glyph_idx):
            out.append(f"                    4'd{c}: glyph = 8'd{g};")
        out.append(f"                    default: glyph = 8'd{used};")   # 空格
        out.append("                endcase")
        out.append("            end")
    out += ["            default: begin time_ms = 32'hFFFFFFFF; glyph = 8'd0; end",
            "        endcase", "    end", "endmodule", "`endif", ""]
    return "\n".join(out)


def emit_geom(chars: list, lines_data: list) -> str:
    return "\n".join([
        "// 由 tools/gen_lrc_assets.py 自动生成，请勿手改。",
        f"localparam integer LRC_LINE_COUNT     = {len(lines_data)};",
        f"localparam integer LRC_CHARS_PER_LINE = {CHARS_PER_LINE};",
        f"localparam integer LRC_GLYPH_W        = {GLYPH_W};",
        f"localparam integer LRC_GLYPH_H        = {GLYPH_H};",
        f"localparam integer LRC_OSD_X          = {OSD_X};",
        f"localparam integer LRC_OSD_Y          = {OSD_Y};",
        f"localparam integer LRC_SCALE          = {SCALE};",
        f"localparam integer LRC_CHARSET_SIZE   = {len(chars)};",
        f"localparam integer LRC_FONT_ADDR_W    = {max(1, (len(chars) * GLYPH_H - 1).bit_length())};",
        f"localparam [23:0]  LRC_COLOR          = 24'h{COLOR[0]:02X}{COLOR[1]:02X}{COLOR[2]:02X};",
        f"localparam [7:0]   LRC_SPACE_GLYPH    = 8'd{chars.index(' ')};",
        "",
    ])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VinylFPGA S5 歌词资源生成器")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--lrc", help="旧版单曲 LRC 输入，保持原资产生成行为")
    source.add_argument("--songs-manifest", help="D9 四曲 UTF-8 资料清单")
    ap.add_argument("--verify", action="store_true", help="额外输出还原预览 PNG")
    ap.add_argument("--out-dir", default=str(ROOT))
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    try:
        if args.songs_manifest:
            if args.verify:
                raise LrcError("--verify 预览仅支持旧版单曲 --lrc 模式")
            manifest_path = Path(args.songs_manifest)
            result = generate_song_text_assets(manifest_path, out)
            line_count = sum(len(track["lines"]) for track in result["tracks"])
            line_depth = max(1, line_count)
            print("SONGS_OK 曲目 4，歌词行数 "
                  f"{line_count}，去重字形 {len(result['chars'])}")
            print("SONGS_ROM_BITS "
                  f"font={result['font_depth'] * GLYPH_W}, "
                  f"lines={line_depth * 165}, "
                  f"metadata={4 * 2 * 16 * 8}")
            for song_id, track in enumerate(result["tracks"]):
                print(f"  song_id={song_id} title={track['title']!r} "
                      f"artist={track['artist']!r} lines={len(track['lines'])}")
            return 0

        lrc_path = Path(args.lrc)
        lines = parse_lrc(lrc_path)
        chars = build_charset(lines)
        glyphs = [render_glyph(ch, ord(ch) < 128) for ch in chars]
        char_index = {ch: i for i, ch in enumerate(chars)}
        space = char_index[" "]

        table = []
        for t_ms, text in lines:
            idx = [char_index[ch] for ch in text]
            table.append((t_ms, idx, space))

        (out / "src/display/lrc_font_rom.v").write_text(emit_font_rom(chars, glyphs), encoding="utf-8")
        (out / "src/display/lrc_lines_rom.v").write_text(emit_lines_rom(table), encoding="utf-8")
        (out / "src/include/lrc_geom.vh").write_text(emit_geom(chars, table), encoding="utf-8")

        print(f"LRC_OK 行数 {len(table)}，字形 {len(chars)}，"
              f"字库 {len(chars) * GLYPH_H * 2} 字节")
        for i, (t_ms, idx, _) in enumerate(table):
            print(f"  [{i}] {t_ms:>6} ms  " + "".join(chars[g] for g in idx))

        if args.verify:
            img = Image.new("RGB", (len(chars) * GLYPH_W, GLYPH_H), (0, 0, 0))
            px = img.load()
            for gi, rows in enumerate(glyphs):
                for y, bits in enumerate(rows):
                    for x in range(GLYPH_W):
                        if bits & (1 << (GLYPH_W - 1 - x)):
                            px[gi * GLYPH_W + x, y] = COLOR
            png = lrc_path.with_name(lrc_path.stem + "_preview.png")
            img.save(png)
            print(f"LRC_OK 预览 {png}")
        return 0
    except LrcError as exc:
        print(f"LRC_FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
