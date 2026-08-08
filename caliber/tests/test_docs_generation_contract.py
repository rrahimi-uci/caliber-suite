"""Contracts for the flattened, machine-readable documentation output.

``llms.txt`` advertises the generated ``m-*.md`` files for programmatic use.
Those files live in one flat directory even though their Markdown sources are
nested under ``docs/``. A source-relative cross-reference can therefore exist
and render correctly in the source tree while being broken in the published
copy. These checks pin the published contract independently of the generator.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site"
DOCS_SOURCE = REPO_ROOT / "docs"
LAYERED_ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
PUBLIC_DOCS = REPO_ROOT / "caliber" / "caliber-ui" / "public" / "docs"
PACKAGED_DOCS = REPO_ROOT / "caliber" / "src" / "caliber" / "ui" / "docs"
MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^\n)]*)\)")
MODULE_ENTRY = re.compile(r'\{\s*md: "([^"]+)",\s*out: "([^"]+\.html)"')


def _destination(raw: str) -> str:
    """Return a Markdown link destination without an optional title."""
    value = raw.strip()
    if value.startswith("<"):
        close = value.find(">")
        return value[1:close] if close >= 0 else value
    return value.split(maxsplit=1)[0] if value else ""


def _local_markdown_links(path: Path) -> list[str]:
    links: list[str] = []
    for match in MARKDOWN_LINK.finditer(path.read_text(encoding="utf-8")):
        destination = _destination(match.group(1))
        parsed = urlsplit(destination)
        if (
            not destination
            or parsed.scheme
            or parsed.netloc
            or destination.startswith(("#", "mailto:", "data:"))
        ):
            continue
        links.append(destination)
    return links


def _manifest_modules() -> list[tuple[str, str]]:
    """Read the public module mapping without executing the mutating JS build."""
    builder = (DOCS_SITE / "build-docs.mjs").read_text(encoding="utf-8")
    return MODULE_ENTRY.findall(builder)


def _without_link_destinations(markdown: str) -> str:
    """Keep authored text/labels while ignoring the expected flattened href rewrite."""

    def replace_destination(match: re.Match[str]) -> str:
        whole = match.group(0)
        start = match.start(1) - match.start(0)
        end = match.end(1) - match.start(0)
        return f"{whole[:start]}<LINK>{whole[end:]}"

    return MARKDOWN_LINK.sub(replace_destination, markdown)


def _normalize_published_markdown(markdown: str, source: Path) -> str:
    """Mirror the generator's removal of repository-only presentation chrome."""
    if source.resolve() != LAYERED_ARCHITECTURE:
        return markdown
    wrappers = {
        '<div align="center">',
        "</div>",
        '<div align="center"><sub>',
        "</sub></div>",
    }
    lines = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped in wrappers:
            continue
        if re.fullmatch(
            r'<img\s+src="docs-site/caliber\.png"[^>]*?/?>',
            stripped,
            flags=re.IGNORECASE,
        ):
            continue
        lines.append(line)
    return "\n".join(lines).replace("&nbsp;", " ")


#: Site files that are not per-module output. Hoisted out of ``_served_site_files``
#: so the coverage assertions below can name what they expect rather than counting.
FIXED_SITE_FILES = frozenset(
    {
        "index.html",
        "docs.css",
        "docs.js",
        "docs-nav.js",
        "llms.txt",
        "presentation.html",
        "presentation_timed.html",
        "walkthrough.html",
    }
)


def _served_site_files(directory: Path) -> set[str]:
    return {
        path.name
        for path in directory.iterdir()
        if path.is_file()
        and (
            path.name in FIXED_SITE_FILES
            or (path.name.startswith("m-") and path.suffix in {".html", ".md"})
        )
    }


def _served_copy(source: Path) -> str:
    content = source.read_text(encoding="utf-8")
    if source.suffix == ".html":
        content = content.replace('"caliber-icon.png"', '"../caliber-icon.png"')
        content = content.replace('"caliber.png"', '"../caliber.png"')
    return content


def test_all_manifest_markdown_is_current_and_published() -> None:
    """Pin every manifest module to its flattened generated Markdown copy.

    Link destinations intentionally change during flattening. Removing only those
    destinations lets this independent test catch stale prose (the failure mode
    that previously left the refinement-loop and roadmap copies behind) while the
    link-resolution test below validates the rewritten destinations themselves.
    """
    modules = _manifest_modules()
    assert modules, "the manifest parsed empty; every assertion below would pass vacuously"
    assert modules[0] == (
        "../ARCHITECTURE.md",
        "m-00-layered-architecture.html",
    )

    for source_name, html_name in modules:
        source = DOCS_SOURCE / source_name
        generated = DOCS_SITE / html_name.replace(".html", ".md")
        assert source.is_file(), source_name
        assert generated.is_file(), generated.name
        assert _without_link_destinations(generated.read_text(encoding="utf-8")) == (
            _without_link_destinations(
                _normalize_published_markdown(source.read_text(encoding="utf-8"), source)
            )
        ), f"stale generated Markdown for {source_name}"


def test_layered_architecture_render_and_links_are_published() -> None:
    """The repository-level map renders cleanly in HTML and flat Markdown."""
    assert LAYERED_ARCHITECTURE.is_file()
    html = (DOCS_SITE / "m-00-layered-architecture.html").read_text(encoding="utf-8")
    markdown = (DOCS_SITE / "m-00-layered-architecture.md").read_text(encoding="utf-8")

    assert '<h1 id="top">CALIBER — Layered Architecture</h1>' in html
    assert '<pre class="mermaid">' in html
    assert "<hr>" in html
    assert "&lt;div align=" not in html
    assert 'href="m-01-platform.html"' in html
    assert 'href="m-17-competitive-analysis.html"' in html

    assert markdown.lstrip().startswith("# CALIBER — Layered Architecture")
    assert '<div align="center">' not in markdown
    assert "docs-site/caliber.png" not in markdown
    assert "(m-01-platform.md)" in markdown
    assert "(m-17-competitive-analysis.md)" in markdown
    assert "(walkthrough.html)" in markdown
    assert (
        "https://github.com/rrahimi-uci/caliber-suite/blob/main/caliber/src/caliber/server.py"
    ) in markdown
    assert (
        "https://github.com/rrahimi-uci/caliber-suite/tree/main/caliber/src/caliber/routes"
    ) in markdown


def test_llms_index_and_flattened_markdown_links_resolve_locally() -> None:
    llms = DOCS_SITE / "llms.txt"
    indexed = {
        unquote(urlsplit(link).path)
        for link in _local_markdown_links(llms)
        if urlsplit(link).path.endswith(".md")
    }
    generated = {path.name for path in DOCS_SITE.glob("m-*.md")}

    assert indexed == generated, (
        "llms.txt must index every generated Markdown module exactly once; "
        f"missing={sorted(generated - indexed)}, stale={sorted(indexed - generated)}"
    )

    broken: list[str] = []
    escaped: list[str] = []
    docs_root = DOCS_SITE.resolve()
    for name in sorted(generated):
        source = DOCS_SITE / name
        for link in _local_markdown_links(source):
            parsed = urlsplit(link)
            target = (source.parent / unquote(parsed.path)).resolve()
            try:
                target.relative_to(docs_root)
            except ValueError:
                escaped.append(f"{name}: {link}")
                continue
            if not target.is_file():
                broken.append(f"{name}: {link}")

    assert not escaped, "flattened Markdown links escape docs-site:\n" + "\n".join(escaped)
    assert not broken, "flattened Markdown links have missing targets:\n" + "\n".join(broken)


def test_the_manifest_and_the_published_site_are_in_bijection() -> None:
    """Every manifest entry is published, and every published module is in the manifest.

    This replaced two count assertions --- ``len(modules) == 21`` and
    ``len(expected) == 67``. A count detects that something moved without saying
    what: adding ``docs/runbook.md`` to the manifest produced ``assert 21 == 20``,
    which names neither the file nor which side changed, and both numbers then had
    to be bumped by hand. It is also satisfiable for the wrong reason, since one
    addition and one removal cancel.

    A bijection carries the information the count was standing in for. Adding a
    module to the manifest without publishing it, publishing one that is not in the
    manifest, or dropping either, each fails with the file named. Nothing needs
    bumping when the set legitimately grows.
    """
    declared = {out for _, out in _manifest_modules()}
    published = {
        path.name
        for path in DOCS_SITE.iterdir()
        if path.is_file() and path.name.startswith("m-") and path.suffix == ".html"
    }
    # Cookbook pages come from ``docs-site/cookbooks/<slug>/`` rather than from
    # the module manifest, so they are checked against their own sources. The
    # index over them is generated too, and is named rather than counted.
    cookbooks = {name for name in published if name.startswith("m-cookbook-")}
    cookbook_index = {"m-16-cookbooks.html"}

    assert declared, "the manifest parsed empty"
    assert declared <= published, (
        f"declared in the manifest but not published: {sorted(declared - published)}"
    )
    unexplained = published - declared - cookbooks - cookbook_index
    assert not unexplained, (
        "published modules that no manifest entry and no cookbook source accounts "
        f"for: {sorted(unexplained)}"
    )

    cookbook_sources = {
        path.name
        for path in (DOCS_SITE / "cookbooks").iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    }
    assert len(cookbooks) == len(cookbook_sources), (
        f"{len(cookbook_sources)} cookbook sources produced {len(cookbooks)} pages; "
        f"sources: {sorted(cookbook_sources)}\n  pages: {sorted(cookbooks)}"
    )

    missing_markdown = {
        name.replace(".html", ".md")
        for name in declared
        if not (DOCS_SITE / name.replace(".html", ".md")).is_file()
    }
    assert not missing_markdown, (
        f"declared modules with no flat Markdown: {sorted(missing_markdown)}"
    )


def test_all_materialized_docs_copies_match_docs_site() -> None:
    """The tracked public tree and any built package tree match the served site."""
    expected = _served_site_files(DOCS_SITE)

    # Coverage, not a count. Without this the equality below is satisfied by two
    # empty directories, which is the failure mode a bare ``len(...) == 67`` was
    # really guarding against -- while also having to be re-typed whenever a
    # module was added.
    declared = {out for _, out in _manifest_modules()}
    assert expected >= FIXED_SITE_FILES, (
        f"site is missing fixed files: {sorted(FIXED_SITE_FILES - expected)}"
    )
    assert declared <= expected, f"site is missing declared modules: {sorted(declared - expected)}"

    assert _served_site_files(PUBLIC_DOCS) == expected

    for name in sorted(expected):
        expected_content = _served_copy(DOCS_SITE / name)
        assert (PUBLIC_DOCS / name).read_text(encoding="utf-8") == expected_content, name

    # ``src/caliber/ui`` is a build output excluded from Git. Check it whenever
    # the UI prebuild/package step has materialized it, without making a clean
    # backend-only checkout depend on an earlier Node build.
    if PACKAGED_DOCS.is_dir():
        assert _served_site_files(PACKAGED_DOCS) == expected
        for name in sorted(expected):
            expected_content = _served_copy(DOCS_SITE / name)
            assert (PACKAGED_DOCS / name).read_text(encoding="utf-8") == expected_content, name
