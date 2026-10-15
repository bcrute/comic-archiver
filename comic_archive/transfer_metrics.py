"""Bounded aggregate asset timings. Never accept URLs, headers or error text."""
from collections import Counter, deque
import math
import threading

NETWORK_FAILURE_CODES = frozenset({'connect_timeout','read_timeout','write_timeout','pool_timeout',
    'connect_error','read_error','write_error','protocol_error','network_error'})


def network_failure_code(error):
    import httpx
    for name, code in [('ConnectTimeout','connect_timeout'),('ReadTimeout','read_timeout'),
                       ('WriteTimeout','write_timeout'),('PoolTimeout','pool_timeout'),
                       ('ConnectError','connect_error'),('ReadError','read_error'),
                       ('WriteError','write_error'),('ProtocolError','protocol_error')]:
        if isinstance(error, getattr(httpx, name)):
            return code
    return 'network_error'


class TransferMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = self.retries = self.active = self.peak = self.successes = self.bytes = 0
        self.statuses, self.errors = Counter(), Counter()
        self.latencies = deque(maxlen=2048)
        self.first = self.last = None
        self.duration_sum = 0.0

    def begin(self, started, retry=False):
        with self.lock:
            self.requests += 1
            self.retries += int(retry)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.first = started if self.first is None else min(self.first, started)

    def finish(self, started, ended, status, byte_count, error=None):
        duration = max(0.0, ended - started)
        with self.lock:
            self.active -= 1
            self.last = ended if self.last is None else max(self.last, ended)
            self.duration_sum += duration
            self.latencies.append(duration)
            if type(status) is int and 100 <= status <= 599:
                self.statuses[str(status)] += 1
            if error is not None:
                # Strict vocabulary even if an adapter is accidentally noisy.
                code = error if isinstance(error,str) and error in NETWORK_FAILURE_CODES | {'http_error','download_limit','unsupported_response'} else 'network_error'
                self.errors[code] += 1
            elif status == 200:
                self.successes += 1
                self.bytes += byte_count

    def snapshot(self):
        with self.lock:
            values = sorted(self.latencies)
            def percentile(fraction):
                return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 4) if values else None
            return {'requests':self.requests,'retries':self.retries,
                    'successful_requests':self.successes,'successful_bytes':self.bytes,
                    'inflight_peak':self.peak,'status_counts':dict(self.statuses),
                    'error_counts':dict(self.errors),
                    'attempt_seconds_sum':round(self.duration_sum, 4),
                    'asset_span_seconds':round(max(0.0,self.last-self.first),4) if self.last is not None else 0.0,
                    'attempt_p50_seconds':percentile(.5),'attempt_p95_seconds':percentile(.95),
                    'latency_sample_count':len(values),'latency_sample_limit':2048}
