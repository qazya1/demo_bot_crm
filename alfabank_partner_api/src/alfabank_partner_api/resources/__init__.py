"""Sub-resources of ``AlfaBankClient``."""
from .checks import AsyncChecksResource, ChecksResource
from .dictionaries import (
    AsyncDictionariesResource,
    DictionariesResource,
    DictionaryCache,
)
from .leads import AsyncLeadsResource, LeadsResource
from .scoring import AsyncScoringResource, ScoringResource

__all__ = [
    "AsyncChecksResource",
    "AsyncDictionariesResource",
    "AsyncLeadsResource",
    "AsyncScoringResource",
    "ChecksResource",
    "DictionariesResource",
    "DictionaryCache",
    "LeadsResource",
    "ScoringResource",
]
