"""Publication fence shared by derived memory files and explicit forgetting."""
from contextlib import nullcontext

from cc_harness.atomic import atomic_write_text


async def epoch_of(store):
    generation = getattr(store, "generation", None)
    return await generation() if callable(generation) else 0


async def publish_derived(store, epoch, files):
    async with getattr(store, "write_lock", nullcontext()):
        if await epoch_of(store) != epoch:
            return False
        for path, body in files:
            atomic_write_text(path, body)
        return True
