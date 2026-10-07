"""Ashby public job board API (used by many YC companies).

https://api.ashbyhq.com/posting-api/job-board/{board}
"""

from __future__ import annotations

from urllib.parse import quote

from ..fetch import get_json
from ..textutil import html_to_text
from .base import Harvester, HarvestTarget, RawJob, TitlePrefilter, parse_timestamp

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyHarvester(Harvester):
    ats_type = "ashby"

    def fetch(self, target: HarvestTarget, title_prefilter: TitlePrefilter | None = None) -> list[RawJob]:
        token = quote(target.board_token.strip(), safe="")
        payload = get_json(self.client, API_URL.format(token=token), params={"includeCompensation": "false"})
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if not item.get("id") or not item.get("title") or item.get("isListed") is False:
                continue
            locations = [item.get("location")] + [
                loc.get("location") for loc in item.get("secondaryLocations") or [] if isinstance(loc, dict)
            ]
            jobs.append(
                RawJob(
                    company=target.name,
                    external_id=str(item["id"]),
                    title=item["title"].strip(),
                    url=item.get("jobUrl") or f"https://jobs.ashbyhq.com/{token}/{item['id']}",
                    ats_type=self.ats_type,
                    location=" / ".join(loc for loc in locations if loc) or None,
                    department=item.get("department") or item.get("team"),
                    employment_type=item.get("employmentType"),
                    description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")),
                    posted_at=parse_timestamp(item.get("publishedAt")),
                    company_domain=target.domain,
                )
            )
        return jobs
