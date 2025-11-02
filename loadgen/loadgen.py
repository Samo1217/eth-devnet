#!/usr/bin/env python3
import os
import time
import asyncio
import contextlib
import httpx
from prometheus_client import start_http_server, Counter, Gauge, Histogram

RPC_URL = os.getenv("RPC_URL", "http://geth-devnet:8545")
TPS = float(os.getenv("TPS", "10"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "50"))
TO_ADDR = os.getenv("TO_ADDR", "0x62358b29b9e3e70ff51D88766e41a339D3e8FFff")
VALUE_ETH = float(os.getenv("VALUE_ETH", "0.0001"))
SAMPLE_WINDOW = float(os.getenv("SAMPLE_WINDOW", "5.0"))

# Per-method read RPS (0 disables)
RPS_BLOCK = float(os.getenv("RPS_BLOCK", "0"))  # eth_blockNumber
RPS_BAL = float(os.getenv("RPS_BAL", "0"))  # eth_getBalance
RPS_CALL = float(os.getenv("RPS_CALL", "0"))  # eth_call

print(
    f"[loadgen] boot: RPC_URL={RPC_URL} TPS={TPS} CONCURRENCY={CONCURRENCY} "
    f"TO={TO_ADDR} VALUE_ETH={VALUE_ETH} SAMPLE_WINDOW={SAMPLE_WINDOW} "
    f"RPS_BLOCK={RPS_BLOCK} RPS_BAL={RPS_BAL} RPS_CALL={RPS_CALL}",
    flush=True,
)

# Prometheus metrics
TX_SENT = Counter("loadgen_tx_sent_total", "Total tx attempts")
TX_OK = Counter("loadgen_tx_ok_total", "Successful tx submissions")
TX_ERR = Counter("loadgen_tx_error_total", "Failed tx submissions")
RPC_REQ = Counter("loadgen_rpc_requests_total", "RPC requests made", ["method"])
RPC_LAT = Histogram(
    "loadgen_rpc_latency_seconds",
    "RPC latency seconds",
    ["method"],
    buckets=(0.01, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 2.0, 5.0),
)
ACH_TPS = Gauge("loadgen_achieved_tps", "Achieved tx/sec")
RPS = Gauge("loadgen_rps", "RPC requests/sec")
MGAS_S = Gauge("loadgen_mgas_per_s", "MegaGas per second (derived)")
FAIL_RT = Gauge("loadgen_failure_rate", "Failure rate (0..1) over window")


def to_wei_hex(eth: float) -> str:
    return hex(int(eth * 10**18))


class Rpc:
    def __init__(self, url: str, timeout: float = 10):
        self.c = httpx.AsyncClient(timeout=timeout)
        self.url = url
        self._id = 0
        self._rpc_counter = 0
        self._lock = asyncio.Lock()

    async def call(self, method: str, params):
        async with self._lock:
            self._id += 1
            rid = self._id
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
        RPC_REQ.labels(method).inc()
        self._rpc_counter += 1
        start = time.perf_counter()
        try:
            r = await self.c.post(self.url, json=payload)
            latency = time.perf_counter() - start
            RPC_LAT.labels(method).observe(latency)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                # expose RPC-level errors via exception so callers can count failures
                raise RuntimeError(data["error"])
            return data["result"]
        except Exception:
            # ensure we still record latency on errors
            RPC_LAT.labels(method).observe(time.perf_counter() - start)
            raise

    def drain_rpc_counter(self):
        v = self._rpc_counter
        self._rpc_counter = 0
        return v

    async def close(self):
        await self.c.aclose()


async def get_sender(rpc: Rpc) -> str:
    accs = await rpc.call("eth_accounts", [])
    print(f"[loadgen] eth_accounts -> {accs}", flush=True)
    if not accs:
        raise RuntimeError(
            "No unlocked accounts returned by eth_accounts; geth --dev should expose one."
        )
    return accs[0]


async def send_tx(rpc: Rpc, sender: str, to_addr: str, value_eth: float):
    TX_SENT.inc()
    try:
        # EIP-1559 fees
        tip = await rpc.call("eth_maxPriorityFeePerGas", [])
        tip_wei = int(tip, 16) if isinstance(tip, str) else int(tip)
        priority = max(tip_wei, 1_000_000_000)  # >= 1 gwei
        maxfee = priority * 2  # simple cap

        params = [
            {
                "from": sender,
                "to": to_addr,
                "value": to_wei_hex(value_eth),
                "gas": hex(21000),
                "maxPriorityFeePerGas": hex(priority),
                "maxFeePerGas": hex(maxfee),
                # legacy alternative:
                # "gasPrice": hex(1_000_000_000),
            }
        ]
        _ = await rpc.call("eth_sendTransaction", params)
        TX_OK.inc()
    except Exception as e:
        TX_ERR.inc()
        print(f"[loadgen] eth_sendTransaction error: {e}", flush=True)
        raise


async def rate_limiter(tps: float):
    interval = 1.0 / tps if tps > 0 else 0.0
    next_t = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now < next_t:
            await asyncio.sleep(next_t - now)
        yield
        next_t += interval


async def sampler(rpc: Rpc, window: float):
    last_ok = TX_OK._value.get()
    last_err = TX_ERR._value.get()

    # prime with the current head
    head = await rpc.call("eth_getBlockByNumber", ["latest", False])
    last_num = int(head["number"], 16)
    last_mgas = 0.0

    while True:
        await asyncio.sleep(window)

        # 1) Achieved TPS & failure rate over the window
        ok = TX_OK._value.get()
        err = TX_ERR._value.get()
        okd = ok - last_ok
        errd = err - last_err
        total = okd + errd
        ACH_TPS.set(okd / window if window > 0 else 0.0)
        FAIL_RT.set((errd / total) if total > 0 else 0.0)
        last_ok, last_err = ok, err

        # 2) RPS from internal counter
        RPS.set(rpc.drain_rpc_counter() / window)

        # 3) MGas/s from new head block (gasUsed / block_time)
        blk = await rpc.call("eth_getBlockByNumber", ["latest", False])
        num = int(blk["number"], 16)

        if num > last_num:
            # fetch parent to get its timestamp
            parent = await rpc.call("eth_getBlockByHash", [blk["parentHash"], False])
            ts = int(blk["timestamp"], 16)
            pts = int(parent["timestamp"], 16)
            dt = max(ts - pts, 1)  # seconds
            gas = int(blk["gasUsed"], 16)  # gas in this block
            last_mgas = (gas / dt) / 1_000_000  # MGas/s for this block
            MGAS_S.set(last_mgas)
            last_num = num
        else:
            # no new block yet; keep the previous per-block MGas/s
            MGAS_S.set(last_mgas)


async def rps_loop(rpc: Rpc, method: str, params_fn, rps: float):
    """Generate a steady per-method request rate (best-effort)."""
    if rps <= 0:
        return
    interval = 1.0 / rps
    next_t = time.perf_counter()
    while True:
        now = time.perf_counter()
        if now < next_t:
            await asyncio.sleep(next_t - now)
        try:
            await rpc.call(method, params_fn())
        except Exception:
            # ignore individual call errors for read traffic
            pass
        next_t += interval


async def run():
    start_http_server(9100)
    rpc = Rpc(RPC_URL, timeout=10)
    try:
        sender = await get_sender(rpc)

        # Warm up labelled series so short-window rates aren't empty
        for m in [
            "eth_sendTransaction",
            "eth_maxPriorityFeePerGas",
            "eth_blockNumber",
            "eth_getBalance",
            "eth_call",
        ]:
            RPC_REQ.labels(m)
            RPC_LAT.labels(m)

        # TX path
        sem = asyncio.Semaphore(CONCURRENCY)
        limiter = rate_limiter(TPS)

        async def one():
            async with sem:
                try:
                    await send_tx(rpc, sender, TO_ADDR, VALUE_ETH)
                except Exception:
                    pass

        # Sampler
        asyncio.create_task(sampler(rpc, SAMPLE_WINDOW))

        # Read-method RPS loops (driven by env vars)
        if RPS_BLOCK > 0:
            asyncio.create_task(rps_loop(rpc, "eth_blockNumber", lambda: [], RPS_BLOCK))
        if RPS_BAL > 0:
            asyncio.create_task(
                rps_loop(rpc, "eth_getBalance", lambda: [sender, "latest"], RPS_BAL)
            )
        if RPS_CALL > 0:
            asyncio.create_task(
                rps_loop(rpc, "eth_call", lambda: [{"to": TO_ADDR}, "latest"], RPS_CALL)
            )

        # Fire the TX generator
        async for _ in limiter:
            asyncio.create_task(one())

    finally:
        with contextlib.suppress(Exception):
            await rpc.close()


if __name__ == "__main__":
    asyncio.run(run())
