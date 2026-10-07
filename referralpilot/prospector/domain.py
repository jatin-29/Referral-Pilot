"""Company domain discovery from posting URLs, config, or DNS-checked guesses."""

from __future__ import annotations

from urllib.parse import urlparse

from ..textutil import slugify

# Hosts that belong to job boards / ATS vendors, never to the employer.
ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com",
    "workday.com", "ycombinator.com", "workatastartup.com", "jobvite.com", "icims.com", "bamboohr.com",
    "recruitee.com", "breezy.hr", "teamtailor.com", "personio.de", "personio.com", "rippling.com",
    "linkedin.com", "indeed.com", "wellfound.com", "angel.co", "naukri.com", "instahyre.com", "cutshort.io",
    "hirist.com", "hirist.tech", "freshteam.com", "zohorecruit.com", "keka.com", "darwinbox.in", "jazzhr.com",
    "applytojob.com", "recruiterbox.com", "glassdoor.com", "dover.com", "gem.com",
}

# Public-suffix entries with two labels that show up for company sites.
TWO_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in",
    "com.au", "net.au", "co.nz", "co.jp", "co.kr", "com.br", "com.cn", "com.hk", "com.sg", "com.my", "com.mx",
    "com.tr", "co.za", "co.id", "com.ph", "com.vn", "co.il", "com.ar", "com.tw", "com.pk", "com.bd",
}

GUESS_TLDS = (".com", ".io", ".ai", ".co", ".dev", ".in", ".app")


def registrable_domain(host: str) -> str:
    host = host.lower().strip().strip(".").split(":")[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_from_url(url: str | None) -> str | None:
    """Employer domain from a posting URL, or None for ATS-hosted pages."""
    if not url:
        return None
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    if not host or "." not in host:
        return None
    domain = registrable_domain(host)
    return None if domain in ATS_DOMAINS else domain


def has_mx(domain: str, timeout: float = 4.0) -> bool | None:
    """True/False when dnspython is installed, None when the check is unavailable."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return None
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        return len(answers) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except dns.exception.DNSException:
        return None


def guess_domains(company: str) -> list[str]:
    """Candidate domains, most likely first: the full name, then without "Labs"/"Inc"/..."""
    base = slugify(company, 40).replace("-", "")
    candidates = [base + tld for tld in GUESS_TLDS]
    for suffix in (" inc", " labs", " technologies", " ai", " hq"):
        if company.lower().endswith(suffix):
            trimmed = slugify(company[: -len(suffix)], 40).replace("-", "")
            if trimmed:
                candidates += [trimmed + tld for tld in GUESS_TLDS]
            break
    return list(dict.fromkeys(candidates))


def resolve_domain(
    company_name: str,
    *,
    configured: str | None = None,
    posting_url: str | None = None,
    use_dns: bool = True,
) -> tuple[str | None, str]:
    """Best-known email domain for a company and where it came from."""
    if configured:
        return configured.lower().strip(), "config"
    from_url = domain_from_url(posting_url)
    if from_url:
        return from_url, "posting_url"
    candidates = guess_domains(company_name)
    if use_dns:
        for candidate in candidates:
            if has_mx(candidate):
                return candidate, "dns_guess"
    return (candidates[0], "guess") if candidates else (None, "unknown")
