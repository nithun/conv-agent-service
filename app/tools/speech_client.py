"""ASR/TTS provider wrappers (CR-004 Task 4), ported from
`tap-buddy/src/tap_buddy/clients/speech_client.py`. Google Speech-to-Text +
Text-to-Speech behind a thin interface, swap-later. Locale/voice resolved
from the language map (`app/config/language_map.py`), never hardcoded.

Low-confidence ASR threshold: 0.5 — below this, the wrapper signals
`low_confidence=True` and callers should trigger a clarification turn
rather than trusting the transcript.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.language_map import LocaleEntry

ASR_CONFIDENCE_THRESHOLD = 0.5


@dataclass
class AsrResult:
    transcript: str
    confidence: float

    @property
    def low_confidence(self) -> bool:
        return self.confidence < ASR_CONFIDENCE_THRESHOLD or not self.transcript.strip()


@dataclass
class TtsResult:
    audio_bytes: bytes
    content_type: str = "audio/mpeg"


class SpeechClient:
    """Thin wrapper around google-cloud-speech + google-cloud-texttospeech.
    Instantiated once per process; the underlying Google clients handle
    their own connection pooling.
    """

    def __init__(self) -> None:
        # Imported lazily so pytest (which mocks this class entirely) never
        # needs google-cloud-speech/texttospeech installed with valid ADC.
        from google.cloud import speech, texttospeech

        self._speech_client = speech.SpeechClient()
        self._tts_client = texttospeech.TextToSpeechClient()
        self._speech = speech
        self._texttospeech = texttospeech

    def transcribe(self, audio_bytes: bytes, locale: LocaleEntry) -> AsrResult:
        """Synchronous recognize call — WhatsApp voice notes are OGG_OPUS."""
        config = self._speech.RecognitionConfig(
            encoding=self._speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=16000,
            language_code=locale.stt_locale,
        )
        audio = self._speech.RecognitionAudio(content=audio_bytes)
        response = self._speech_client.recognize(config=config, audio=audio)
        if not response.results:
            return AsrResult(transcript="", confidence=0.0)
        top = response.results[0].alternatives[0]
        return AsrResult(transcript=top.transcript, confidence=top.confidence)

    def synthesize(self, text: str, locale: LocaleEntry) -> TtsResult:
        synthesis_input = self._texttospeech.SynthesisInput(text=text)
        voice = self._texttospeech.VoiceSelectionParams(
            language_code=locale.tts_language_code, name=locale.tts_voice
        )
        audio_config = self._texttospeech.AudioConfig(
            audio_encoding=self._texttospeech.AudioEncoding.MP3
        )
        response = self._tts_client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        return TtsResult(audio_bytes=response.audio_content, content_type="audio/mpeg")
