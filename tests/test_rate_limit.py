import asyncio

import discord

from guildmaster.utils.rate_limit import ChannelOpQueue, QueuedOp


class FakeRateLimit(discord.HTTPException):
    def __init__(self, retry_after=0.0):
        self.status = 429
        self.retry_after = retry_after


class FakeForbidden(discord.Forbidden):
    def __init__(self):
        self.status = 403


async def test_queue_processes_all_ops_in_order():
    queue = ChannelOpQueue(min_delay=0)
    order = []

    async def make(i):
        order.append(i)
        return i

    await queue.start()
    ops = [QueuedOp(label=str(i), factory=lambda i=i: make(i)) for i in range(5)]
    for op in ops:
        await queue.submit(op)
    await queue.join()
    await queue.stop()

    assert order == [0, 1, 2, 3, 4]
    assert all(op.error is None for op in ops)
    assert [op.result for op in ops] == [0, 1, 2, 3, 4]


async def test_queue_retries_on_rate_limit():
    queue = ChannelOpQueue(min_delay=0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeRateLimit(retry_after=0.0)
        return "ok"

    await queue.start()
    op = QueuedOp(label="flaky", factory=flaky)
    await queue.submit(op)
    await queue.join()
    await queue.stop()

    assert calls["n"] == 2
    assert op.result == "ok"
    assert op.error is None


async def test_queue_captures_forbidden_without_retry():
    queue = ChannelOpQueue(min_delay=0)
    calls = {"n": 0}

    async def denied():
        calls["n"] += 1
        raise FakeForbidden()

    await queue.start()
    op = QueuedOp(label="denied", factory=denied)
    await queue.submit(op)
    await queue.join()
    await queue.stop()

    assert calls["n"] == 1
    assert isinstance(op.error, discord.Forbidden)


async def test_queue_keeps_going_after_failure():
    queue = ChannelOpQueue(min_delay=0)

    async def ok():
        return "fine"

    async def bad():
        raise RuntimeError("nope")

    await queue.start()
    op1 = QueuedOp(label="bad", factory=bad)
    op2 = QueuedOp(label="ok", factory=ok)
    await queue.submit(op1)
    await queue.submit(op2)
    await queue.join()
    await queue.stop()

    assert isinstance(op1.error, RuntimeError)
    assert op2.result == "fine"


async def test_progress_callback_fires():
    events = []
    queue = ChannelOpQueue(
        min_delay=0,
        on_progress=lambda op, done, total: events.append((op.label, done, total))
        or asyncio.sleep(0),
    )
    await queue.start()
    for i in range(3):
        await queue.submit(QueuedOp(label=str(i), factory=lambda: asyncio.sleep(0)))
    await queue.join()
    await queue.stop()
    assert [e[1:] for e in events] == [(1, 3), (2, 3), (3, 3)]
