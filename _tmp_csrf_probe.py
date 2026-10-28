"""Temporary recon probe: which frontend files POST to CSRF-exempt prefixes without a token."""
import pathlib
import re

ROOT = pathlib.Path(r"D:\Work\LUMIVST\frontend")
DIRS = [ROOT / "app", ROOT / "components", ROOT / "lib", ROOT / "hooks", ROOT / "utils", ROOT / "store"]
MUT = re.compile(r"method:\s*['\"](POST|PUT|PATCH|DELETE)['\"]", re.I)
REBH = re.compile(r"api/(rebh|engine)")

rows = []
for d in DIRS:
    if not d.exists():
        continue
    for p in d.rglob("*.tsx"):
        rows.append(p)
    for p in d.rglob("*.ts"):
        rows.append(p)

for path in sorted(rows):
    text = path.read_text(encoding="utf-8", errors="replace")
    muts = MUT.findall(text)
    if not muts:
        continue
    uses_rebh = bool(REBH.search(text))
    if not uses_rebh:
        continue
    has_csrf = bool(re.search(r"x-csrf-token", text))
    has_authfetch = bool(re.search(r"authFetch|apiClient", text))
    print(
        f"{'CSRF_HDR' if has_csrf else 'NO_HDR '} "
        f"{'authFetch/apiClient' if has_authfetch else 'plain-fetch       '} "
        f"muts={len(muts)} {path.relative_to(ROOT)}"
    )
