"""Skill lexicon: canonical skill names, categories and the patterns that detect them.

Patterns are case-insensitive unless a skill is marked case-sensitive; scoped
flags such as `(?-i:...)` handle words that are ambiguous in plain English
("React", "Go", "Express", "REST").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Skill:
    name: str
    category: str
    patterns: tuple[str, ...]
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _s(name: str, category: str, *patterns: str, aliases: tuple[str, ...] = ()) -> Skill:
    return Skill(name, category, patterns or (rf"\b{re.escape(name.lower())}\b",), aliases)


LANG, FE, BE, DB, CLOUD, DATA, MOBILE, CONCEPT = (
    "Languages", "Frontend", "Backend", "Databases", "Cloud & DevOps", "Data & ML", "Mobile", "Concepts",
)

SKILLS: tuple[Skill, ...] = (
    # Languages
    _s("Python", LANG, r"\bpython\b"),
    _s("Java", LANG, r"\bjava\b"),
    _s("JavaScript", LANG, r"\bjava\s*script\b", r"\becmascript\b", r"(?-i:(?<![.\w])JS\b)", r"\bes6\b"),
    _s("TypeScript", LANG, r"\btype\s*script\b"),
    _s("C++", LANG, r"(?<![\w+])c\+\+(?![\w+])", r"\bcpp\b"),
    _s("C#", LANG, r"(?<![\w#])c#(?![\w#])", r"\bc\s*sharp\b"),
    _s("C", LANG, r"(?-i:\bC(?=\s*(?:/|,|\band\b|\bor\b)\s*C\+\+))", r"(?-i:\bC\s+programming\b)",
       r"(?-i:\bin\s+C\b(?![+#]))", aliases=("c", "c language")),
    # "Go" is only the language mid-sentence ("in Go", ", Go"), in lists ("Go/Rust", "(Go)")
    # or at a line start not followed by a verb-like word ("Go above and beyond" is ignored).
    _s("Go", LANG, r"\bgolang\b",
       r"(?-i:(?<=[a-z0-9,;:(/&-] )Go(?![\w-]))",
       r"(?-i:(?<=[/(])Go(?![\w-]))",
       r"(?-i:(?<![\w.-])Go(?=\s*[,/)]))",
       r"(?-i:(?<![\w.-])Go(?![\w-])(?!\s+(?:to|live|beyond|above|through|out|back|ahead|deep|further|from|with|on|get|build|make|the|a|an)\b))",
       aliases=("go", "golang")),
    _s("Rust", LANG, r"\brust\b"),
    _s("Kotlin", LANG, r"\bkotlin\b"),
    _s("Swift", LANG, r"(?-i:\bSwift(?:UI)?\b)", aliases=("swift",)),
    _s("Ruby", LANG, r"\bruby\b(?!\s+on\s+rails)"),
    _s("PHP", LANG, r"\bphp\b"),
    _s("Scala", LANG, r"\bscala\b"),
    _s("SQL", LANG, r"\bsql\b"),
    _s("Bash", LANG, r"\bbash\b", r"\bshell\s+script(?:ing|s)?\b"),
    _s("Lua", LANG, r"\blua\b"),
    # Frontend
    _s("HTML", FE, r"\bhtml5?\b"),
    _s("CSS", FE, r"\bcss3?\b", r"\bscss\b", r"\bsass\b"),
    _s("React", FE, r"(?-i:\bReact(?:\.?js|JS)?\b)(?!\s+[Nn]ative)", r"\breact\.?js\b", aliases=("react",)),
    _s("Angular", FE, r"\bangular(?:\.?js)?\b"),
    _s("Vue.js", FE, r"\bvue(?:\.?js)?\b"),
    _s("Next.js", FE, r"\bnext\.?js\b"),
    _s("Redux", FE, r"\bredux\b"),
    _s("Svelte", FE, r"\bsvelte(?:kit)?\b"),
    _s("Tailwind CSS", FE, r"\btailwind(?:\s*css)?\b"),
    _s("Accessibility", FE, r"\baccessibility\b", r"\ba11y\b", r"\bwcag\b"),
    _s("Electron", FE, r"\belectron\b"),
    # Backend
    _s("Node.js", BE, r"\bnode\s*\.?\s*js\b", r"(?-i:\bNode\b)(?=\s*(?:/|,|\)|and\b|or\b|backend|services?))",
       aliases=("node", "nodejs")),
    _s("Express", BE, r"\bexpress\.?js\b", r"(?-i:\bExpress\b)", aliases=("express",)),
    _s("NestJS", BE, r"\bnest\.?js\b"),
    _s("Django", BE, r"\bdjango\b"),
    _s("Flask", BE, r"\bflask\b"),
    _s("FastAPI", BE, r"\bfast\s*api\b"),
    _s("Spring Boot", BE, r"\bspring\s*boot\b", r"(?-i:\bSpring\b)(?!\s+(?:semester|term|break|intern))",
       aliases=("spring",)),
    _s("Ruby on Rails", BE, r"\b(?:ruby\s+on\s+)?rails\b"),
    _s(".NET", BE, r"(?<![\w.])\.net\b", r"\basp\.net\b", r"\bdotnet\b"),
    _s("REST APIs", BE, r"\brestful\b", r"\brest(?:ful)?\s*(?:apis?|services?|endpoints?|interfaces?)\b",
       r"(?-i:\bREST\b)", aliases=("rest", "rest api", "restful apis")),
    _s("GraphQL", BE, r"\bgraphql\b"),
    _s("gRPC", BE, r"\bgrpc\b"),
    _s("Microservices", BE, r"\bmicro-?services?\b"),
    _s("WebSockets", BE, r"\bweb\s*-?sockets?\b"),
    _s("Kafka", BE, r"\bkafka\b"),
    _s("RabbitMQ", BE, r"\brabbit\s*mq\b"),
    _s("Celery", BE, r"\bcelery\b"),
    # Databases
    _s("PostgreSQL", DB, r"\bpostgres(?:ql)?\b"),
    _s("MySQL", DB, r"\bmysql\b"),
    _s("MongoDB", DB, r"\bmongo(?:db)?\b"),
    _s("Redis", DB, r"\bredis\b"),
    _s("Cassandra", DB, r"\bcassandra\b"),
    _s("DynamoDB", DB, r"\bdynamo\s*db\b"),
    _s("Elasticsearch", DB, r"\belastic\s*search\b", r"\bopensearch\b"),
    _s("SQLite", DB, r"\bsqlite\b"),
    _s("ClickHouse", DB, r"\bclickhouse\b"),
    _s("NoSQL", DB, r"\bnosql\b"),
    _s("Prisma", DB, r"\bprisma\b"),
    # Cloud & DevOps
    _s("AWS", CLOUD, r"\baws\b", r"\bamazon\s+web\s+services\b", r"\bec2\b"),
    _s("GCP", CLOUD, r"\bgcp\b", r"\bgoogle\s+cloud\b"),
    _s("Azure", CLOUD, r"\bazure\b"),
    _s("Docker", CLOUD, r"\bdocker\b"),
    _s("Kubernetes", CLOUD, r"\bkubernetes\b", r"\bk8s\b", r"\beks\b", r"\bgke\b"),
    _s("Terraform", CLOUD, r"\bterraform\b"),
    _s("CI/CD", CLOUD, r"\bci\s*/\s*cd\b", r"\bcontinuous\s+(?:integration|delivery|deployment)\b",
       r"\bgithub\s+actions\b", r"\bjenkins\b"),
    _s("Linux", CLOUD, r"\blinux\b", r"\bunix\b"),
    _s("Git", CLOUD, r"\bgit\b", r"\bgithub\b", r"\bgitlab\b"),
    _s("Prometheus", CLOUD, r"\bprometheus\b", r"\bgrafana\b"),
    _s("Nginx", CLOUD, r"\bnginx\b"),
    _s("Serverless", CLOUD, r"\bserverless\b", r"\baws\s+lambda\b"),
    # Data & ML
    _s("Machine Learning", DATA, r"\bmachine\s+learning\b", r"(?-i:\bML\b)", aliases=("ml",)),
    _s("PyTorch", DATA, r"\bpytorch\b"),
    _s("TensorFlow", DATA, r"\btensorflow\b"),
    _s("Pandas", DATA, r"\bpandas\b"),
    _s("NumPy", DATA, r"\bnumpy\b"),
    _s("Spark", DATA, r"\b(?:apache\s+)?spark\b", r"\bpyspark\b"),
    _s("Airflow", DATA, r"\bairflow\b"),
    _s("LLMs", DATA, r"\bllms?\b", r"\blarge\s+language\s+models?\b", r"\bgen(?:erative)?\s*ai\b"),
    _s("Data Pipelines", DATA, r"\bdata\s+pipelines?\b", r"\betl\b", r"\bingestion\s+pipelines?\b"),
    # Mobile
    _s("Android", MOBILE, r"\bandroid\b"),
    _s("iOS", MOBILE, r"\bios\b"),
    _s("Flutter", MOBILE, r"\bflutter\b"),
    _s("React Native", MOBILE, r"\breact\s+native\b"),
    # Concepts
    _s("Data Structures & Algorithms", CONCEPT, r"\bdata\s+structures?\b", r"\balgorithms?\b", r"\bdsa\b",
       aliases=("dsa", "data structures", "algorithms", "data structures and algorithms")),
    _s("System Design", CONCEPT, r"\bsystem\s+design\b", r"\bsystems?\s+architecture\b"),
    _s("Distributed Systems", CONCEPT, r"\bdistributed\s+(?:systems?|computing)\b"),
    _s("OOP", CONCEPT, r"\boop\b", r"\bobject[\s-]oriented\b"),
    _s("Operating Systems", CONCEPT, r"\boperating\s+systems?\b"),
    _s("Computer Networks", CONCEPT, r"\bcomputer\s+networks?\b", r"\bnetworking\b", r"\btcp/ip\b"),
    _s("DBMS", CONCEPT, r"\bdbms\b", r"\bdatabase\s+(?:management|design|internals)\b"),
    _s("Concurrency", CONCEPT, r"\bconcurren(?:cy|t)\b", r"\bmulti-?threading\b"),
    _s("Caching", CONCEPT, r"\bcach(?:e|es|ing)\b"),
    _s("Message Queues", CONCEPT, r"\bmessage\s+(?:queues?|brokers?)\b", r"\bpub\s*/?\s*sub\b"),
    _s("Testing", CONCEPT, r"\bunit\s+test(?:s|ing)?\b", r"\bintegration\s+tests?\b", r"\btest[\s-]driven\b",
       r"\btdd\b", r"\bjest\b", r"\bpytest\b", r"\bjunit\b"),
    _s("Raft", CONCEPT, r"\braft\b"),
)

SKILL_BY_NAME: dict[str, Skill] = {skill.name: skill for skill in SKILLS}


@lru_cache(maxsize=1)
def _compiled() -> tuple[tuple[Skill, tuple[re.Pattern, ...]], ...]:
    return tuple((skill, tuple(re.compile(p, re.IGNORECASE) for p in skill.patterns)) for skill in SKILLS)


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for skill in SKILLS:
        index[skill.name.lower()] = skill.name
        for alias in skill.aliases:
            index[alias.lower()] = skill.name
    return index


@dataclass(frozen=True)
class SkillMention:
    skill: str
    start: int
    end: int


def find_mentions(text: str) -> list[SkillMention]:
    """All non-overlapping skill mentions in `text`, ordered by position."""
    found: list[SkillMention] = []
    for skill, patterns in _compiled():
        for pattern in patterns:
            for match in pattern.finditer(text):
                if match.end() > match.start():
                    found.append(SkillMention(skill.name, match.start(), match.end()))
    # Prefer longer matches when spans overlap ("React Native" over "React").
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    result: list[SkillMention] = []
    last_end = -1
    for mention in found:
        if mention.start >= last_end:
            result.append(mention)
            last_end = mention.end
    return result


def extract_skills(text: str) -> set[str]:
    return {mention.skill for mention in find_mentions(text)}


def normalize_skill(raw: str) -> set[str]:
    """Map a free-text skill ("Postgres", "Spring Boot", "C/C++") to canonical names.

    Unknown skills are returned unchanged so custom skills still count.
    """
    cleaned = raw.strip()
    if not cleaned:
        return set()
    alias = _alias_index().get(cleaned.lower())
    if alias:
        return {alias}
    found = extract_skills(cleaned)
    return found or {cleaned}


def category_of(skill: str) -> str:
    entry = SKILL_BY_NAME.get(skill)
    return entry.category if entry else "Other"
