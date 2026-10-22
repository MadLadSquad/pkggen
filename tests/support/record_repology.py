"""Record repology's answers for test data: python -m tests.support.record_repology <project>...

Writes tests/support/data/repology.json ({project: [package]}), trimmed to the
releases in distributions.yaml, so the offline tests use real shapes. Projects
already in the file are kept; pass --refresh to fetch them again.
"""
import json
import sys
import time

import requests
import yaml

from tests.support import paths

OUTPUT = paths.REPO / "tests" / "support" / "data" / "repology.json"
KEYS = ("repo", "subrepo", "srcname", "binname", "binnames", "visiblename", "version", "status", "summary")


def releases():
    with open(paths.GENERATORS / "distributions" / "distributions.yaml", encoding="utf-8") as f:
        return {rp for dist in yaml.safe_load(f)["distributions"] for rp in dist["rp-names"]}


def main(args):
    refresh = "--refresh" in args
    names = [a for a in args if not a.startswith("--")]
    data = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    known = releases()
    for name in names:
        if name in data and not refresh:
            continue
        response = requests.get(f"https://repology.org/api/v1/project/{name}", timeout=30,
                                headers={"User-Agent": "pkggen-tests (+https://github.com/MadLadSquad/pkggen)"})
        response.raise_for_status()
        data[name] = [{k: p[k] for k in KEYS if p.get(k) is not None}
                      for p in response.json() if p.get("repo") in known]
        print(f"{name}: {len(data[name])} packages", file=sys.stderr)
        time.sleep(1.1)
    OUTPUT.write_text(json.dumps(dict(sorted(data.items())), indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
