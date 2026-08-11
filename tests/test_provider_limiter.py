"""Provider API 并发 limiter 的独立测试。"""

import threading
import time

import pytest

from provider import Provider, ProviderCallLimiter
from provider import provider as provider_module


def test_provider_limiter_caps_parallel_calls_and_releases_permits():
    limiter = ProviderCallLimiter(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker():
        nonlocal active, peak
        assert limiter.acquire()
        try:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
        finally:
            with lock:
                active -= 1
            limiter.release()

    workers = [threading.Thread(target=worker) for _ in range(5)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=2)

    assert peak == 2
    assert limiter.acquire(interrupt_check=lambda: True) is False
    assert limiter.acquire()
    limiter.release()


def test_provider_releases_limiter_permit_after_request_error(monkeypatch):
    limiter = ProviderCallLimiter(1)
    provider = Provider.__new__(Provider)
    provider._call_limiter = limiter
    provider._stream_once = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("upstream failed")
    )
    monkeypatch.setattr(provider_module, "RETRY_API_MAX_RETRIES", 0)

    with pytest.raises(RuntimeError, match="upstream failed"):
        provider.stream(messages=[], tools=[])

    assert limiter.acquire()
    limiter.release()
