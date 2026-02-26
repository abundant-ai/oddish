from __future__ import annotations

import secrets
import string

_ADJECTIVES = [
    "amber",
    "brisk",
    "calm",
    "crisp",
    "daring",
    "eager",
    "frosty",
    "glossy",
    "jolly",
    "lucky",
    "mellow",
    "nimble",
    "proud",
    "quiet",
    "rapid",
    "rustic",
    "sable",
    "swift",
    "tidy",
    "vivid",
    "witty",
    "zen",
]

_NOUNS = [
    "atlas",
    "canyon",
    "comet",
    "delta",
    "ember",
    "forge",
    "harbor",
    "haven",
    "meadow",
    "orchard",
    "otter",
    "pine",
    "quartz",
    "ridge",
    "river",
    "signal",
    "summit",
    "valley",
    "vista",
    "willow",
]


def generate_experiment_name() -> str:
    """Generate a short, human-friendly experiment name."""
    adjective = secrets.choice(_ADJECTIVES)
    noun = secrets.choice(_NOUNS)
    suffix = "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4)
    )
    return f"{adjective}-{noun}-{suffix}"
