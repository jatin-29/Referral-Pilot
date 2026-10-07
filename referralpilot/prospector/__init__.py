"""Module C: company domain discovery, email-pattern guessing and contact enrichment."""

from .domain import domain_from_url, registrable_domain, resolve_domain
from .patterns import EmailGuess, generate_candidates, infer_pattern, split_name
from .providers import PROVIDERS, ContactCandidate, ProspectContext, build_queries, search_links
from .service import ProspectResult, add_manual_contact, prospect_job, update_contact_email

__all__ = [
    "PROVIDERS",
    "ContactCandidate",
    "EmailGuess",
    "ProspectContext",
    "ProspectResult",
    "add_manual_contact",
    "build_queries",
    "domain_from_url",
    "generate_candidates",
    "infer_pattern",
    "prospect_job",
    "registrable_domain",
    "resolve_domain",
    "search_links",
    "split_name",
    "update_contact_email",
]
