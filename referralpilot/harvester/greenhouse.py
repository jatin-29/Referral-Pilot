"""Greenhouse Job Board API: https://boards-api.greenhouse.io/v1/boards/{token}/jobs"""

from __future__ import annotations

from urllib.parse import quote

from ..fetch import get_json
from ..textutil import html_to_text
from .base import Harvester, HarvestTarget, RawJob, TitlePrefilter, parse_timestamp

API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseHarvester(Harvester):
    ats_type = "greenhouse"

    def fetch(self, target: HarvestTarget, title_prefilter: TitlePrefilter | None = None) -> list[RawJob]:
        token = quote(target.board_token.strip(), safe="")
        payload = get_json(self.client, API_URL.format(token=token), params={"content": "true"})
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if "id" not in item or not item.get("title"):
                continue
            location = (item.get("location") or {}).get("name")
            departments = [d.get("name") for d in item.get("departments") or [] if d.get("name")]
            job_id = str(item["id"])
            title = item["title"].strip()
            wanted = title_prefilter is None or title_prefilter(title)
            jobs.append(
                RawJob(
                    company=target.name,
                    external_id=job_id,
                    title=title,
                    url=item.get("absolute_url") or f"https://boards.greenhouse.io/{token}/jobs/{job_id}",
                    ats_type=self.ats_type,
                    location=location,
                    department=", ".join(departments) or None,
                    employment_type=_metadata_value(item, "employment type"),
                    # The filter rejects other titles anyway: skip converting their HTML.
                    description=html_to_text(item.get("content")) if wanted else "",
                    posted_at=parse_timestamp(item.get("first_published") or item.get("updated_at")),
                    company_domain=target.domain,
                )
            )
        return jobs


def _metadata_value(item: dict, name: str) -> str | None:
    for meta in item.get("metadata") or []:
        if str(meta.get("name", "")).lower() == name and isinstance(meta.get("value"), str):
            return meta["value"]
    return None
