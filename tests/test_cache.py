import asyncio
from datetime import date

from shared.cache import JsonCache, cache_key


def test_key_is_deterministic_and_order_independent():
    a = cache_key("https://x/api", {"q": "egfr", "n": 5}, date(2021, 3, 1))
    b = cache_key("https://x/api", {"n": 5, "q": "egfr"}, "2021-03-01")
    assert a == b
    assert a != cache_key("https://x/api", {"q": "egfr", "n": 5}, date(2022, 3, 1))


async def test_fetch_calls_live_once_then_serves_cache(tmp_path):
    cache = JsonCache(tmp_path)
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"hits": [1, 2, 3]}

    results = await asyncio.gather(*[cache.fetch("ep", {"q": 1}, fetcher) for _ in range(5)])
    assert calls == 1
    assert all(r.data == {"hits": [1, 2, 3]} for r in results)

    again = await cache.fetch("ep", {"q": 1}, fetcher)
    assert again.from_cache is True
    assert again.fetched_at == results[0].fetched_at
    assert calls == 1


def test_corrupt_entry_is_a_miss(tmp_path):
    cache = JsonCache(tmp_path)
    resp = cache.set("k" * 64, {"a": 1})
    path = tmp_path / resp.key[:2] / f"{resp.key}.json"
    path.write_text("{not json")
    assert cache.get(resp.key) is None
