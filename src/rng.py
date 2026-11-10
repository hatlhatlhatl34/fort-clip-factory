"""Seeded randomness — same seed, same choices, forever.

Ported concept from forever-factory: every clip decision is a pure
function of (day, slot, source id), so a crashed run re-executes the
identical render instead of a different one."""
from __future__ import annotations

import hashlib


def seed_for(*parts) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big")


def pick(rng, seq):
    """Random element of seq via a seeded random.Random."""
    return rng.choice(list(seq))
