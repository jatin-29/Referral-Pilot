#!/usr/bin/env python3
"""Build the static GitHub Pages site: the dashboard running in the browser on Pyodide.

    python scripts/build_web.py --out site [--jobs jobs.json] [--pyodide auto|self-host|cdn]

Output (everything is static; the page runs the Python app in a Web Worker):

    index.html, web/        page shell, request bridge and worker
    static/                 the dashboard's CSS and JavaScript (same files a local install serves)
    app.zip                 the referralpilot package, resume templates and config
    pyodide-lock.json       the Pyodide packages the app needs, plus the bundled wheels below
    pyodide/                Pyodide runtime and packages (self-host mode)
    wheels/                 pure-Python wheels that Pyodide does not ship (sqlmodel, fpdf2, ...)
    jobs.json               public postings from the scheduled crawl (with --jobs)
    manifest.json           versions and hashes the page reads at start-up

Pyodide modes: "self-host" copies the runtime (npm) and Pyodide's own package builds (jsDelivr)
into the site, so nothing loads from a third party at run time; "pypi" self-hosts too but takes
the packages from PyPI (pure-Python wheels plus PyPI's WebAssembly build of pydantic-core), for
when jsDelivr is unreachable; "cdn" points browsers at jsDelivr. "auto" tries self-host, then
pypi, then cdn. --pyodide-dir uses a local Pyodide distribution and --lock-overrides supplies
replacement wheels for lock packages (offline tests).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYODIDE_VERSION = "314.0.7"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
PYODIDE_NPM = f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE_VERSION}.tgz"
CORE_FILES = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")

# Pyodide packages the app imports at run time (their dependencies come from the lock file).
PYODIDE_PACKAGES = (
    "fastapi", "starlette", "pydantic", "pydantic-core", "sqlalchemy", "httpx", "httpcore", "h11", "certifi",
    "idna", "anyio", "sniffio", "jinja2", "markupsafe", "beautifulsoup4", "soupsieve", "typing-extensions",
    "annotated-types", "typing-inspection", "annotated-doc", "tzdata", "fonttools",
)

# Pure-Python wheels Pyodide does not ship: (distribution, version, import names, depends).
EXTRA_WHEELS = (
    ("sqlmodel", "0.0.48", ["sqlmodel"], ["sqlalchemy", "pydantic", "typing-extensions"]),
    ("pydantic-settings", "2.15.0", ["pydantic_settings"], ["pydantic", "python-dotenv", "typing-inspection"]),
    ("python-dotenv", "1.2.4", ["dotenv"], []),
    ("python-multipart", "0.0.32", ["python_multipart", "multipart"], []),
    ("fpdf2", "2.8.9", ["fpdf"], ["defusedxml", "fonttools"]),
    ("defusedxml", "0.7.1", ["defusedxml"], []),
)

# "pypi" mode: versions that differ from the Pyodide lock. PyPI only has WebAssembly builds of
# recent pydantic-core releases, which need this pydantic and newer typing helpers.
PYPI_PINS = {"pydantic": "2.14.0b2", "pydantic-core": "2.49.0", "typing-inspection": "0.4.4",
             "typing-extensions": "4.16.0"}

APP_SOURCES = ("referralpilot", "templates", "config")
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
ZIP_TIME = (2024, 1, 1, 0, 0, 0)  # fixed timestamps: the archive hash only changes with its content


def log(message: str) -> None:
    print(f"[build_web] {message}", flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def download(url: str, *, expect_sha256: str | None = None, cache: Path | None = None) -> bytes:
    cached = cache / hashlib.sha1(url.encode()).hexdigest() if cache else None
    if cached and cached.exists():
        data = cached.read_bytes()
        if not expect_sha256 or sha256(data) == expect_sha256:
            return data
    request = urllib.request.Request(url, headers={"User-Agent": "referralpilot-build"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if expect_sha256 and sha256(data) != expect_sha256:
        raise RuntimeError(f"checksum mismatch for {url}")
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return data


# --- app bundle -------------------------------------------------------------------------

def build_app_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for top in APP_SOURCES:
            for path in sorted((ROOT / top).rglob("*")):
                rel = path.relative_to(ROOT)
                if path.is_dir() or SKIP_PARTS & set(rel.parts) or path.suffix in {".pyc", ".pyo"}:
                    continue
                info = zipfile.ZipInfo(rel.as_posix(), ZIP_TIME)
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


# --- Pyodide ----------------------------------------------------------------------------

def core_files(pyodide_dir: Path | None, cache: Path) -> dict[str, bytes]:
    if pyodide_dir:
        return {name: (pyodide_dir / name).read_bytes() for name in CORE_FILES}
    log(f"downloading Pyodide {PYODIDE_VERSION} runtime from npm")
    tarball = download(PYODIDE_NPM, cache=cache)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.split("/", 1)[-1]
            if name in CORE_FILES and member.isfile():
                files[name] = archive.extractfile(member).read()
    missing = set(CORE_FILES) - set(files)
    if missing:
        raise RuntimeError(f"npm package lacks {sorted(missing)}")
    return files


def dependency_closure(packages: dict, wanted: tuple[str, ...]) -> list[str]:
    seen: list[str] = []
    stack = list(wanted)
    while stack:
        name = normalize(stack.pop())
        if name in seen:
            continue
        if name not in packages:
            raise RuntimeError(f"{name} is not in the Pyodide {PYODIDE_VERSION} lock file")
        seen.append(name)
        stack.extend(packages[name].get("depends", []))
    return sorted(seen)


def wheel_metadata(data: bytes) -> tuple[str, str]:
    """(name, version) from a wheel's METADATA."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        meta_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        text = archive.read(meta_name).decode("utf-8", "replace")
    name = re.search(r"^Name: (.+)$", text, re.M).group(1).strip()
    version = re.search(r"^Version: (.+)$", text, re.M).group(1).strip()
    return name, version


def load_overrides(directory: Path | None) -> dict[str, tuple[str, bytes]]:
    overrides: dict[str, tuple[str, bytes]] = {}
    if directory:
        for wheel in sorted(directory.glob("*.whl")):
            data = wheel.read_bytes()
            name, _ = wheel_metadata(data)
            overrides[normalize(name)] = (wheel.name, data)
    return overrides


def pypi_release(name: str, version: str) -> dict:
    return json.loads(download(f"https://pypi.org/pypi/{name}/{version}/json", cache=None))


def pypi_wheel(name: str, version: str, cache: Path, abi: str | None = None) -> tuple[str, bytes]:
    """A pure-Python wheel (or, with `abi`, a Pyodide WebAssembly wheel) from PyPI."""
    meta = pypi_release(name, version)
    for item in meta["urls"]:
        filename = item["filename"]
        if item["packagetype"] != "bdist_wheel":
            continue
        if filename.endswith("-none-any.whl") or (abi and f"pyemscripten_{abi}_wasm32" in filename):
            return filename, download(item["url"], expect_sha256=item["digests"]["sha256"], cache=cache)
    if name == "markupsafe":
        return pure_markupsafe_wheel(meta, cache)
    raise RuntimeError(f"no usable wheel on PyPI for {name}=={version}")


def pure_markupsafe_wheel(meta: dict, cache: Path) -> tuple[str, bytes]:
    """MarkupSafe without its optional C speedups, packed from the sdist (PyPI has no pure wheel)."""
    import base64

    version = meta["info"]["version"]
    sdist = next(item for item in meta["urls"] if item["packagetype"] == "sdist")
    data = download(sdist["url"], expect_sha256=sdist["digests"]["sha256"], cache=cache)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            parts = member.name.split("/")
            if (member.isfile() and len(parts) >= 4 and parts[1:3] == ["src", "markupsafe"]
                    and not member.name.endswith((".c", ".pyi"))):
                files["markupsafe/" + "/".join(parts[3:])] = archive.extractfile(member).read()
    dist = f"markupsafe-{version}.dist-info"
    files[f"{dist}/METADATA"] = f"Metadata-Version: 2.1\nName: MarkupSafe\nVersion: {version}\n".encode()
    files[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: build_web\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = [f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b'=').decode()},"
              f"{len(body)}" for path, body in files.items()]
    files[f"{dist}/RECORD"] = ("\n".join([*record, f"{dist}/RECORD,,"]) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            info = zipfile.ZipInfo(path, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    return f"markupsafe-{version}-py3-none-any.whl", buffer.getvalue()


def build_pyodide(out: Path, mode: str, pyodide_dir: Path | None, overrides_dir: Path | None,
                  cache: Path) -> dict:
    official_core = core_files(pyodide_dir, cache) if mode != "cdn" else None
    if official_core is None:
        lock_source = (pyodide_dir / "pyodide-lock.json").read_bytes() if pyodide_dir else download(
            PYODIDE_CDN + "pyodide-lock.json", cache=cache)
    else:
        lock_source = official_core["pyodide-lock.json"]
    official = json.loads(lock_source)
    packages = {normalize(k): v for k, v in official["packages"].items()}
    names = dependency_closure(packages, PYODIDE_PACKAGES)
    overrides = load_overrides(overrides_dir)

    lock_entries: dict[str, dict] = {}
    index_url = PYODIDE_CDN
    if mode != "cdn":
        target = out / "pyodide"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"):
            (target / name).write_bytes(official_core[name])
        # The runtime also reads the stock lock file from indexURL; keep it next to the core.
        (target / "pyodide-lock.json").write_bytes(lock_source)
        index_url = "pyodide/"
    abi = official["info"].get("abi_version")
    for name in names:
        entry = dict(packages[name])
        if mode == "cdn":
            entry["file_name"] = PYODIDE_CDN + entry["file_name"]
        else:
            local = pyodide_dir / entry["file_name"] if pyodide_dir else None
            if local and local.exists():
                data = local.read_bytes()
            elif name in overrides or mode == "pypi":
                if name in overrides:
                    file_name, data = overrides[name]
                else:
                    file_name, data = pypi_wheel(name, PYPI_PINS.get(name, entry["version"]), cache, abi)
                _, version = wheel_metadata(data)
                if version != entry["version"]:
                    log(f"  {name}: {version} instead of {entry['version']} ({file_name})")
                entry.update(file_name=file_name, version=version, sha256=sha256(data))
            else:
                data = download(PYODIDE_CDN + entry["file_name"], expect_sha256=entry["sha256"], cache=cache)
            (out / "pyodide" / entry["file_name"]).write_bytes(data)
            entry["file_name"] = "pyodide/" + entry["file_name"]
        lock_entries[name] = entry

    wheels_dir = out / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    for dist, version, imports, depends in EXTRA_WHEELS:
        file_name, data = pypi_wheel(dist, version, cache)
        (wheels_dir / file_name).write_bytes(data)
        lock_entries[normalize(dist)] = {
            "name": normalize(dist), "version": version, "file_name": f"wheels/{file_name}",
            "install_dir": "site", "sha256": sha256(data), "package_type": "package", "imports": imports,
            "depends": [normalize(d) for d in depends], "unvendored_tests": False, "tool": {},
        }

    lock = {"info": official["info"], "packages": dict(sorted(lock_entries.items())), "tool": official.get("tool", {})}
    lock_bytes = json.dumps(lock, indent=1, sort_keys=True).encode()
    (out / "pyodide-lock.json").write_bytes(lock_bytes)
    return {
        "version": PYODIDE_VERSION,
        "mode": mode,
        "indexURL": index_url,
        "lockFile": f"pyodide-lock.json?v={sha256(lock_bytes)[:16]}",
        "packages": sorted(lock_entries),
    }


# --- site -------------------------------------------------------------------------------

def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "dev"


def build(args: argparse.Namespace) -> Path:
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cache = Path(args.cache).resolve()

    shutil.copytree(ROOT / "referralpilot" / "ui" / "static", out / "static")
    (out / "web").mkdir()
    for name in ("bridge.js", "worker.js", "web.css"):
        shutil.copyfile(ROOT / "web" / name, out / "web" / name)

    app = build_app_zip()
    (out / "app.zip").write_bytes(app)
    log(f"app.zip: {len(app) / 1024:.0f} KiB")

    attempts = ("self-host", "pypi", "cdn") if args.pyodide == "auto" else (args.pyodide,)
    pyodide = None
    for number, mode in enumerate(attempts, 1):
        try:
            pyodide = build_pyodide(out, mode, args.pyodide_dir, args.lock_overrides, cache)
            break
        except Exception as exc:  # network trouble: try the next source
            if number == len(attempts):
                raise
            log(f"Pyodide packages via {mode} failed ({exc}); trying {attempts[number]}")
            shutil.rmtree(out / "pyodide", ignore_errors=True)
    log(f"Pyodide {pyodide['version']} ({pyodide['mode']}): {len(pyodide['packages'])} packages")

    jobs_info = None
    if args.jobs and Path(args.jobs).exists():
        shutil.copyfile(args.jobs, out / "jobs.json")
        data = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
        jobs_info = {"file": "jobs.json", "generated_at": data.get("generated_at"), "count": len(data.get("jobs", []))}
        log(f"jobs.json: {jobs_info['count']} postings")

    sys.path.insert(0, str(ROOT))
    from referralpilot import __version__

    commit = git_commit()
    build_id = sha256(app + json.dumps(pyodide, sort_keys=True).encode()
                      + b"".join((ROOT / "web" / n).read_bytes() for n in ("bridge.js", "worker.js", "web.css"))
                      + b"".join(p.read_bytes() for p in sorted((out / "static").iterdir())))[:12]
    manifest = {
        "name": "ReferralPilot",
        "version": __version__,
        "commit": commit,
        "build": build_id,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app": {"file": "app.zip", "sha256": sha256(app)},
        "pyodide": pyodide,
        "jobs": jobs_info,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("__BUILD__", build_id)
    (out / "index.html").write_text(index, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    log(f"site ready in {out} ({size / 1024 / 1024:.1f} MiB, build {build_id})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="site", help="output directory (replaced)")
    parser.add_argument("--jobs", help="jobs.json snapshot from `referralpilot export-jobs`")
    parser.add_argument("--pyodide", choices=("auto", "self-host", "pypi", "cdn"), default="auto")
    parser.add_argument("--pyodide-dir", type=Path, help="local Pyodide distribution (core files, maybe wheels)")
    parser.add_argument("--lock-overrides", type=Path, help="wheels that replace lock packages (offline tests)")
    parser.add_argument("--cache", default=os.environ.get("RP_BUILD_CACHE", str(ROOT / ".cache" / "web-build")))
    build(parser.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
