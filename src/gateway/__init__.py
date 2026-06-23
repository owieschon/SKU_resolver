"""Conversational service gateway — chat/voice surface over the verified
machinery (resolution + fulfillment + customer DB). Implements
docs/CONVERSATIONAL_GATEWAY_SPEC.md G1-G8 with the §2.5 hardening:
identification != authorization, discriminating readback, transcript PII
scrubbing, fresh session security, anaphora context, eval-as-blocking-gate.

Availability/lead-time are ungated; pricing is gated behind account
verification AND a separate authorization decision. Voice runs through a
pluggable ASR seam (SimulatedASR in CI; Twilio+AssemblyAI in production).
"""
from gateway.answers import PricingRefused, availability, pricing
from gateway.asr_streaming import (
    AssemblyAIStreamingASR,
    SimulatedStreamingASR,
    StreamingASR,
    StreamingSession,
    parse_turn_message,
    run_stream_turns,
)
from gateway.connector import WebhookConnector, tools_manifest
from gateway.customer_db import CustomerDB, InMemoryCustomerDB
from gateway.db_adapters import SqliteCustomerDB, SqlitePriceBook
from gateway.identification import (
    classify_confirmation,
    identify,
    looks_like_anaphora,
)
from gateway.journal import ConversationJournal, EventType
from gateway.models import (
    Account,
    AuthorizationDecision,
    AvailabilityAnswer,
    Candidate,
    Channel,
    ConfirmationStrength,
    Escalation,
    EscalationReason,
    IdentifiedSKU,
    PriceAnswer,
    SessionState,
    TurnResponse,
)
from gateway.orchestrator import Gateway
from gateway.persona import ACCENT_VOICES, ACCENTS, VoicePersona
from gateway.pricebook import PriceBook, SyntheticPriceBook
from gateway.session import (
    NEUTRAL_REFUSAL,
    SessionManager,
    VerificationResult,
)
from gateway.shadow import (
    CapabilityMap,
    ContinuousImprovement,
    CorrectionStore,
    FailurePoint,
    ImprovementOpportunity,
    ReviewBatch,
    SelfHeal,
    ShadowAttempt,
    ShadowCampaign,
    ShadowObserver,
    looks_part_like,
    opportunity_from,
)
from gateway.shadow_stream import ShadowStreamBridge
from gateway.tts import TTS, SimulatedTTS
from gateway.voice import (
    ASR,
    CONFIDENCE_FLOOR,
    SimulatedASR,
    Transcript,
    keyterms_from_catalog,
    transcript_is_usable,
)
from gateway.voice_stream import (
    TwilioEvent,
    TwilioMediaStream,
    mulaw_decode,
    mulaw_encode,
    parse_twilio_event,
    twilio_mark,
    twilio_media_messages,
)

__all__ = [
    'PricingRefused', 'availability', 'pricing', 'WebhookConnector',
    'tools_manifest', 'CustomerDB', 'InMemoryCustomerDB',
    'classify_confirmation', 'identify', 'looks_like_anaphora',
    'ConversationJournal', 'EventType', 'Account', 'AuthorizationDecision',
    'AvailabilityAnswer', 'Candidate', 'Channel', 'ConfirmationStrength',
    'Escalation', 'EscalationReason',
    'IdentifiedSKU', 'PriceAnswer', 'SessionState', 'TurnResponse', 'Gateway',
    'PriceBook', 'SyntheticPriceBook', 'NEUTRAL_REFUSAL', 'SessionManager',
    'VerificationResult', 'ASR', 'CONFIDENCE_FLOOR', 'SimulatedASR',
    'Transcript', 'keyterms_from_catalog', 'transcript_is_usable',
    'TwilioEvent', 'TwilioMediaStream', 'mulaw_decode', 'mulaw_encode',
    'parse_twilio_event', 'twilio_media_messages', 'twilio_mark',
    'TTS', 'SimulatedTTS', 'ACCENTS', 'ACCENT_VOICES', 'VoicePersona',
    'CapabilityMap', 'ContinuousImprovement', 'CorrectionStore', 'FailurePoint',
    'ImprovementOpportunity', 'ReviewBatch', 'SelfHeal', 'ShadowAttempt',
    'ShadowCampaign', 'ShadowObserver', 'ShadowStreamBridge', 'looks_part_like',
    'opportunity_from',
    'AssemblyAIStreamingASR', 'SimulatedStreamingASR', 'StreamingASR',
    'StreamingSession', 'parse_turn_message', 'run_stream_turns',
    'SqliteCustomerDB', 'SqlitePriceBook',
]
