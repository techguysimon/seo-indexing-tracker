from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def chunk_sequence(sequence: Sequence[T], chunk_size: int) -> list[list[T]]:
    return [
        list(sequence[index : index + chunk_size])
        for index in range(0, len(sequence), chunk_size)
    ]
