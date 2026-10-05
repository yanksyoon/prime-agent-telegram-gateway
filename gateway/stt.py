"""E3T2: mock speech-to-text engine seam.

Real STT (whisper/faster-whisper) is OUT OF SCOPE for Epic 3 (project_init.md
Task 3.2). This module provides the injected seam: the gateway handler calls
``stt_engine.transcribe(file_path)`` where ``stt_engine`` is reachable through
``handlers._get_stt()``. Production wiring uses ``MockSttEngine``, which returns
a fixed placeholder string so the voice->transcription->daemon pipeline is fully
exercised without any heavy model dependency. Tests replace the seam with an
``AsyncMock`` returning a canned transcription.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class SttEngine(Protocol):
    """Protocol for a transcription engine.

    ``transcribe`` is async so a future real engine (e.g. faster-whisper running
    in a worker) fits the same seam without changing callers.
    """

    async def transcribe(self, file_path: str) -> str: ...


class MockSttEngine:
    """A transcription engine that returns a fixed placeholder string.

    Exercises the full download / transcribe / forward pipeline deterministically
    and offline. Swapped out for a real STT engine by a later task.
    """

    async def transcribe(self, file_path: str) -> str:
        logger.info("mock_stt: transcribing %s", file_path)
        return "[voice transcription placeholder]"


mock_stt_engine = MockSttEngine()