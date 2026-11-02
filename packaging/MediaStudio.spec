from pathlib import Path
ROOT=Path(SPECPATH).parent
datas=[(str(ROOT/name),name) for name in ('web','vendor','profiles','resources')]
a=Analysis([str(ROOT/'src/launcher.py')],pathex=[str(ROOT/'src')],binaries=[],datas=datas,
           hiddenimports=['dataclasses','PIL.ImageFont','PIL.ImageDraw','PIL.BmpImagePlugin',
                          'PIL.PngImagePlugin','PIL.JpegImagePlugin','PIL.WebPImagePlugin'],
           excludes=['tkinter','numpy','matplotlib','IPython','playwright','pytest'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='VinylFPGA_MediaStudio',
        debug=False,bootloader_ignore_signals=False,strip=False,upx=False,console=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='VinylFPGA_MediaStudio')
