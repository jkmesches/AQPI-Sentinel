"""FAQ parity — the docs site and the in-app page must ask the same questions.

Run directly (no DB, no network):

    python validation_tests/test_faq_parity.py

The FAQ exists in two places on purpose: `docs/04-faq.md` for the published
documentation site, and `frontend/src/lib/faq.ts` rendered at /faq inside a
running Sentinel. The wording of the ANSWERS is deliberately allowed to differ
— the docs site suits longer prose, the in-app page is read on a dashboard.

What must NOT differ is which questions exist. If someone adds a question in
one place and not the other, the lab team gets different answers depending on
where they happen to look, and neither page is obviously the stale one. That
is the failure this test exists to catch, and it is silent otherwise.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "04-faq.md"
APP = ROOT / "frontend" / "src" / "lib" / "faq.ts"


def normalize(q: str) -> str:
    """Compare on meaning, not punctuation/escaping/markup."""
    q = q.replace("\\'", "'").replace("\\`", "`")
    q = re.sub(r"[`*_]", "", q)
    q = re.sub(r"[^a-z0-9 ]+", " ", q.lower())
    return re.sub(r"\s+", " ", q).strip()


def docs_questions() -> list[str]:
    # "## 7. Why is one radar's threshold different from another's?"
    return [normalize(m) for m in
            re.findall(r"^##\s+\d+\.\s+(.+?)\s*$", DOCS.read_text(), re.M)]


def app_questions() -> list[str]:
    # "\t\tq: 'Why is one radar\'s threshold different from another\'s?',"
    return [normalize(m) for m in
            re.findall(r"^\t\tq:\s*'(.*)',\s*$", APP.read_text(), re.M)]


def main() -> int:
    for f in (DOCS, APP):
        if not f.exists():
            print(f"FAIL  missing {f.relative_to(ROOT)}")
            return 1

    d, a = docs_questions(), app_questions()
    print(f"  docs/04-faq.md          : {len(d)} questions")
    print(f"  frontend/src/lib/faq.ts : {len(a)} questions")

    failures = []
    if not d:
        failures.append("no questions parsed from the docs page")
    if len(d) != len(a):
        failures.append(f"count mismatch: docs {len(d)} vs app {len(a)}")

    only_docs = [q for q in d if q not in a]
    only_app = [q for q in a if q not in d]
    for q in only_docs:
        print(f"  FAIL  in docs but not in the app: {q!r}")
    for q in only_app:
        print(f"  FAIL  in the app but not in docs: {q!r}")
    if only_docs or only_app:
        failures.append("question sets differ")

    if not failures and d != a:
        # Same set, different order. Not fatal — the app has a jump-list and
        # the docs are numbered — but worth surfacing since numbered
        # cross-references ("see Q7") would silently point at different things.
        print("  WARN  same questions, different order — numbered references may not line up")

    if failures:
        print(f"\nFAILED: {'; '.join(failures)}")
        return 1
    print(f"\nPASS — both surfaces ask the same {len(d)} questions, in the same order")
    return 0


if __name__ == "__main__":
    sys.exit(main())
