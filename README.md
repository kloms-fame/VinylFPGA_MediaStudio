# VinylFPGA Media Studio

双击 **启动工具.cmd**，打开独立的 Windows 离线素材、镜像、FPGA 原始像素预览和 SD 写卡工具。

先读 [使用说明](使用说明.md)，核验范围见 [验收报告](验收报告.md)。当前四曲素材和已校验镜像已预置在 `projects/`。复制空白便携工具可使用 `release/` 下的 ZIP。

预览读取最终镜像和实际点阵 ROM。12 组固定状态的 RTL 校对为 0 差异像素；“实板逐像素对照”支持原始 640×480 PNG 零容差比较。更换中文文字或歌词需要重新编译烧录配套 ROM，不能只写卡。运行中板卡实况同步不属于本次交付。

源码、测试、固定固件快照、构建脚本、许可证和验证证据均保存在本文件夹。原 FPGA 主工程不依赖此工具运行。

## 仓库内容与边界

本仓库只放工具本身的源码、测试、脚本与文档：`src/`、`web/`、`scripts/`、`tests/`、`vendor/`、`profiles/`、`packaging/`、`evidence/screenshots/` 与若干 JSON 记录。

以下内容属于体积或版权原因不入库，可在本地由素材重新生成：

| 目录 | 内容 | 生成方式 |
| --- | --- | --- |
| `projects/`、`release/`、`dist/` | 已打包的四曲工程、便携版 ZIP、前端构建产物 | 按《使用说明》重新打包 / `build.py` |
| `resources/` | 字体与便携运行时 | 见 `packaging/` |
| `evidence/rtl_final/` | RTL 逐像素对照的中间大文件 | 跑对照脚本重新生成 |

工具与固件的对应关系：本仓库生成的 `INDEX.BIN`、`PICn.BMP`、`TRACKn.MV`、`LYRICS.BIN` 由固件仓库 [VinylFPGA_Base](https://github.com/kloms-fame/VinylFPGA_Base) 的 `song_index_db` 与 `fat32_media_loader` 解析；点阵 ROM 必须与固件同版本，换字或换歌词要重新编译烧录。
"# VinylFPGA_MediaStudio" 
