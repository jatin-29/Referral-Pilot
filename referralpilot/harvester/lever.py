"""Lever Postings API: https://api.lever.co/v0/postings/{company}?mode=json"""

from __future__ import annotations

from urllib.parse import quote

from ..fetch import get_json
from ..textutil import html_to_text, normalize_text
from .base import Harvester, HarvestTarget, RawJob, TitlePrefilter, parse_timestamp

API_URLS = {
    None: "https://api.lever.co/v0/postings/{token}",
    "eu": "https://api.eu.lever.co/v0/postings/{token}",
}


class LeverHarvester(Harvester):
    ats_type = "lever"

    def fetch(self, target: HarvestTarget, title_prefilter: TitlePrefilter | None = None) -> list[RawJob]:
        token = quote(target.board_token.strip(), safe="")
        url = API_URLS.get((target.region or "").lower() or None, API_URLS[None]).format(token=token)
        payload = get_json(self.client, url, params={"mode": "json"})
        if isinstance(payload, dict):  # error envelope, e.g. {"ok": false, "error": "..."}
            payload = payload.get("data") or []
        jobs: list[RawJob] = []
        for item in payload:
            if not item.get("id") or not item.get("text"):
                continue
            categories = item.get("categories") or {}
            location = categories.get("location") or ", ".join(categories.get("allLocations") or []) or None
            title = item["text"].strip()
            wanted = title_prefilter is None or title_prefilter(title)
            jobs.append(
                RawJob(
                    company=target.name,
                    external_id=str(item["id"]),
                    title=title,
                    url=item.get("hostedUrl") or f"https://jobs.lever.co/{token}/{item['id']}",
                    ats_type=self.ats_type,
                    location=location,
                    department=categories.get("team") or categories.get("department"),
                    employment_type=categories.get("commitment"),
                    description=_description(item) if wanted else "",  # other titles are filtered out anyway
                    posted_at=parse_timestamp(item.get("createdAt")),
                    company_domain=target.domain,
                )
            )
        return jobs


def _description(item: dict) -> str:
    parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
    for section in item.get("lists") or []:
        heading = (section.get("text") or "").strip()
        body = html_to_text(f"<ul>{section.get('content') or ''}</ul>")
        parts.append(f"{heading}\n{body}" if heading else body)
    parts.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
    return normalize_text("\n\n".join(part for part in parts if part))
