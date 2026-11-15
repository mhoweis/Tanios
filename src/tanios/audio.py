"""Converting audio between what WhatsApp uses and what speech models use.

WhatsApp voice notes are Ogg files with the Opus codec, mono. Speech models take WAV
most reliably and give back MP3 or raw PCM. ffmpeg does the conversion.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Protocol

# A voice note longer than this is cut. Five minutes of 16 kHz mono WAV is about 10 MB.
MAX_INPUT_SECONDS = 300
WAV_BYTES_PER_SECOND = 16000 * 2
CONVERT_TIMEOUT_SECONDS = 60


class AudioError(Exception):
    """The audio could not be converted. The message never contains file contents."""


class AudioConverter(Protocol):
    """Tests use a stand-in with this shape (tests/fakes.py)."""

    async def to_wav(self, audio: bytes) -> bytes:
        """Any audio WhatsApp sends, as 16 kHz mono WAV."""
        ...

    async def to_voice_note(self, audio: bytes, audio_format: str) -> bytes:
        """MP3 or raw PCM from a speech model, as the Ogg Opus that WhatsApp plays."""
        ...


def wav_seconds(wav: bytes) -> float:
    """Length of a 16 kHz mono 16-bit WAV, near enough for estimating cost."""
    return max(len(wav) - 44, 0) / WAV_BYTES_PER_SECOND


class FfmpegConverter:
    def __init__(self, ffmpeg: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg

    async def to_wav(self, audio: bytes) -> bytes:
        return await self._run(
            audio, [], ["-ac", "1", "-ar", "16000", "-t", str(MAX_INPUT_SECONDS)], "out.wav"
        )

    async def to_voice_note(self, audio: bytes, audio_format: str) -> bytes:
        # Raw PCM has no header, so ffmpeg must be told what it is: OpenRouter's speech
        # endpoint sends 16-bit little-endian mono at 24 kHz.
        reading = ["-f", "s16le", "-ar", "24000", "-ac", "1"] if audio_format == "pcm" else []
        writing = ["-ac", "1", "-c:a", "libopus", "-b:a", "24k", "-application", "voip"]
        return await self._run(audio, reading, writing, "out.ogg")

    async def _run(
        self, audio: bytes, reading: list[str], writing: list[str], out_name: str
    ) -> bytes:
        if not audio:
            raise AudioError("empty audio")
        with tempfile.TemporaryDirectory(prefix="tanios-audio-") as folder:
            source = Path(folder) / "in"
            target = Path(folder) / out_name
            source.write_bytes(audio)
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *reading,
                "-i",
                str(source),
                "-vn",
                *writing,
                str(target),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                raise AudioError("ffmpeg is not installed") from None
            try:
                await asyncio.wait_for(process.wait(), timeout=CONVERT_TIMEOUT_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise AudioError("conversion took too long") from None
            if process.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                raise AudioError("ffmpeg could not convert the audio")
            return target.read_bytes()
