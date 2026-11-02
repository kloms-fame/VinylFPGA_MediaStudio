"""Portable entry point; resources and user projects have independent roots."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import webbrowser

sys.path.insert(0,str(Path(__file__).resolve().parent))


def main():
    parser=argparse.ArgumentParser(description='VinylFPGA Media Studio · 离线素材与写卡工具')
    parser.add_argument('--workspace',type=Path)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--ready-file',type=Path)
    parser.add_argument('--writer',type=Path)
    args=parser.parse_args()
    if args.writer:
        from studio.devices import write_request
        return write_request(args.writer)
    root=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    workspace=(args.workspace or root/'projects').resolve()
    workspace.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=str(workspace/'studio.log'),level=logging.INFO,encoding='utf-8')
    from studio.server import StudioServer
    server=StudioServer(workspace,args.port)
    ready=dict(url=server.base_url,workspace=str(workspace),pid=os.getpid())
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True,exist_ok=True)
        args.ready_file.write_text(json.dumps(ready),encoding='utf-8')
    if sys.stdout:
        print(json.dumps(ready),flush=True)
    if not args.no_browser:
        webbrowser.open(server.base_url)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        logging.exception('Startup failed')
        if getattr(sys,'frozen',False) and os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc),'Media Studio 启动失败',0x10)
        raise
