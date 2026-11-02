"""Small asynchronous job registry. A failure is terminal and remains inspectable."""
import copy
import threading
import time
import uuid


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._events = {}

    def start(self, function, *, kind='task', project=None):
        key = uuid.uuid4().hex
        event = threading.Event()
        item = dict(id=key, kind=kind, project=project, status='running', stage='starting',
                    fraction=0, message='准备中', error=None, result=None)
        with self._lock:
            if any(x['status'] == 'running' and x['project'] == project for x in self._items.values()):
                raise ValueError('当前项目已有任务运行，请等待完成或取消')
            self._items[key] = item
            self._events[key] = event

        def progress(stage, fraction=0, message=''):
            with self._lock:
                item.update(stage=str(stage), fraction=max(0, min(1, float(fraction))), message=str(message))

        def run():
            try:
                result = function(progress, event)
                with self._lock:
                    item.update(status='done', fraction=1, stage='done', message='完成', result=result)
            except Exception as exc:
                with self._lock:
                    item.update(status='cancelled' if event.is_set() else 'failed',
                                stage='cancelled' if event.is_set() else 'failed', error=str(exc), message=str(exc))
        threading.Thread(target=run, name='studio-'+key[:8], daemon=True).start()
        return key

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._items[key])

    def all(self):
        with self._lock:
            return copy.deepcopy(list(self._items.values())[-30:])

    def cancel(self, key):
        with self._lock:
            item = self._items[key]
            if item['kind'] == 'physical-write':
                raise ValueError('物理写卡已启动；请保持设备连接，等待写入与回读结束')
            self._events[key].set()

    def wait(self, key, timeout=30):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            item = self.get(key)
            if item['status'] != 'running':
                return item
            time.sleep(.02)
        raise TimeoutError(key)
