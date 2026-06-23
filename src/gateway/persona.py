"""Voice persona — the agent's name, speaking style, accent, and voice.

Fully operator-configurable (no hardcoded character): the persona supplies the
spoken greeting text and selects the TTS voice. Accent is modelled as a choice
of voice (how TTS providers actually expose regional English), so each accent
maps to a configurable voice id; an explicit voice id always overrides.

Pure data (no network) so it lives in the gateway; the live TTS adapter that
consumes `resolved_voice_id()` is in the runtime layer.
"""
from __future__ import annotations

from dataclasses import dataclass

# Supported accents. `british`/`australian` are genuinely distinct accents in the
# ElevenLabs stock library; the US-regional slots (northeast/midwest/southern/
# west_coast) default to real, DISTINCT American voices because ElevenLabs stock
# only labels accent at american/british/australian granularity — a TRUE US
# regional accent needs a curated or cloned voice supplied via
# SKU_VOICE_ID_<ACCENT>. Slot selection + override are top-level; the regional
# default is clearly "an American voice", not a claim of regional accent.
ACCENTS = ('standard', 'northeast', 'midwest', 'southern', 'west_coast',
           'british', 'australian')

# Real ElevenLabs voice ids (verified present in the account, 2026-06-07).
ACCENT_VOICES: dict[str, str] = {
    'standard':   'EXAVITQu4vr4xnSDxMaL',   # Sarah — American, reassuring (F)
    'northeast':  'cjVigY5qzO86Huf0OWal',   # Eric — American, smooth (M)
    'midwest':    'pqHfZKP75CvOlQylNhV4',   # Bill — American, wise/balanced (M)
    'southern':   'nPczCjzI2devNBz1zQrb',   # Brian — American, deep/comforting (M)
    'west_coast': 'bIHbv24MWmeRgasZH58o',   # Will — American, relaxed (M)
    'british':    'JBFqnCBsd6RMkjVDRZzb',   # George — British (M)
    'australian': 'IKne3meq5aSn9XLyUdCD',   # Charlie — Australian (M)
}


@dataclass(frozen=True)
class VoicePersona:
    name: str = 'the parts department'
    rep_name: str = ''        # the rep's first name ("...this is Sam"); '' = nameless
    accent: str = 'standard'
    voice_id: str = ''        # explicit override; else derived from accent
    style: str = 'friendly, concise, professional'
    greeting: str = ''        # explicit override; else derived from name

    def __post_init__(self):
        if self.accent not in ACCENTS:
            object.__setattr__(self, 'accent', 'standard')

    def resolved_voice_id(self) -> str:
        return self.voice_id or ACCENT_VOICES.get(self.accent,
                                                  ACCENT_VOICES['standard'])

    def opening(self) -> str:
        if self.greeting:
            return self.greeting
        # A named rep gives a fixed, consistent name in the greeting — so the
        # agent never has to (and must not) invent one per call.
        if self.rep_name:
            return (f'Thank you for calling {self.name}, this is '
                    f'{self.rep_name}. How can I help you today?')
        return f'Thank you for calling {self.name}. How can I help you today?'
