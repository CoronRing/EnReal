# voice_synth.py - Stub implementation for voice synthesis
from pathlib import Path


class BosonVoiceSynth:
    """Stub implementation of BosonVoiceSynth"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def synthesize(
        self,
        text: str,
        reference_wav_path: Path,
        reference_transcript: str,
    ) -> bytes:
        """
        Stub method that returns empty bytes.
        In a real implementation, this would call the Boson API.
        """
        raise NotImplementedError("Voice synthesis not implemented. Use 'silence' provider instead.")


def write_wav(path: Path, data: bytes) -> None:
    """Write WAV data to file"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
