"""Bundle resources and constrained project-relative input paths."""
import hashlib
import importlib.util
import sys
import threading
from pathlib import Path, PureWindowsPath

_VENDOR_LOCK = threading.RLock()


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def sha256_file(path, *, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while True:
            if cancel is not None and cancel.is_set(): raise ValueError('用户已取消生成')
            data = stream.read(4 << 20)
            if not data: break
            digest.update(data)
    return digest.hexdigest()


def safe_relative(value):
    if not isinstance(value, str):
        raise ValueError('素材路径必须是相对路径字符串')
    path = PureWindowsPath(value)
    if value and (path.is_absolute() or path.drive or path.root or ':' in value or
                  any(part == '..' for part in path.parts) or '\x00' in value):
        raise ValueError('拒绝越界素材路径：' + value)
    return value.replace('\\', '/')


def resolve_input(project_dir, value, *, required=True):
    relative = safe_relative(value)
    if not relative:
        if required:
            raise ValueError('缺少素材路径')
        return None
    root = Path(project_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('素材不存在或超出项目目录：' + relative)
    return path


def vendor_module(name):
    if name not in ('vinyl_packer', 'gen_lrc_assets', 'safe_raw_writer'):
        raise ValueError('未知便携组件')
    with _VENDOR_LOCK:
        key = '_studio_vendor_' + name
        if key not in sys.modules:
            path = resource_root() / 'vendor' / (name + '.py')
            if not path.is_file():
                raise ValueError('便携包缺少组件：' + str(path))
            spec = importlib.util.spec_from_file_location(key, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(key, None)
                raise
        return sys.modules[key]


def tool_path(name):
    path = resource_root() / 'resources/bin' / (name + '.exe')
    if not path.is_file():
        raise ValueError('便携包缺少转换程序：' + str(path))
    return path
