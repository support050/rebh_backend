"""Recover xbrl_mapping.py from pycache dump."""
import json
import marshal
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT.parent if ROOT.name == "scripts" else ROOT
SERVICES = BACKEND / "app" / "services"
DUMP = BACKEND / "_map_dump.json"
PYC = SERVICES / "__pycache__" / "xbrl_mapping.cpython-311.pyc"

if not DUMP.exists():
    data_bytes = PYC.read_bytes()
    code = marshal.loads(data_bytes[16:])
    mod = types.ModuleType("xbrl_mapping")
    exec(code, mod.__dict__)
    DUMP.write_text(
        json.dumps(
            {
                "PARAM_MAPPING": {k: list(v) for k, v in mod.PARAM_MAPPING.items()},
                "STANDARD_TEMPLATE": mod.STANDARD_TEMPLATE,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

data = json.loads(DUMP.read_text(encoding="utf-8"))
lines = [
    "# Standardized Template Codes and Mapping for Commercial Companies",
    "# Covers Income Statement (IS), Balance Sheet (BS), and Cash Flow (CF)",
    "",
    "import re",
    "from difflib import SequenceMatcher",
    "",
    "STANDARD_TEMPLATE = {",
]
for code_k, info in data["STANDARD_TEMPLATE"].items():
    lines.append(f"    {code_k!r}: {info!r},")
lines.append("}")
lines.append("")
lines.append("PARAM_MAPPING = {")
for k, v in data["PARAM_MAPPING"].items():
    lines.append(f"    {k!r}: ({v[0]!r}, {v[1]}),")
lines.append("}")
lines.append("")
lines.append(
    '''
FUZZY_THRESHOLD = 0.92

_SWAP_PHRASES = (
    ("associates and joint ventures", "joint ventures and associates"),
    ("joint ventures and associates", "associates and joint ventures"),
)


def normalize_label(label: str) -> str:
    if not label:
        return ""
    s = str(label).replace("\\xa0", " ").lower().strip()
    s = re.sub(r"\\s*\\[abstract\\]", "", s, flags=re.I)
    s = re.sub(r"\\s*\\[line items\\]", "", s, flags=re.I)
    s = re.sub(r"\\s*\\[member\\]", "", s, flags=re.I)
    s = s.replace(",", " ")
    s = re.sub(r"\\s*/\\s*", " / ", s)
    s = re.sub(r"\\s+", " ", s).strip()
    return s


def _token_key(label: str) -> str:
    tokens = sorted(t for t in normalize_label(label).replace("/", " ").split() if t)
    return " ".join(tokens)


def _build_lookup_indexes():
    norm_map = {}
    token_map = {}
    ambiguous = set()
    for key, mapping in PARAM_MAPPING.items():
        nk = normalize_label(key)
        if nk and nk not in norm_map:
            norm_map[nk] = mapping
        tk = _token_key(key)
        if not tk:
            continue
        if tk in token_map and token_map[tk][0] != mapping[0]:
            ambiguous.add(tk)
        elif tk not in token_map:
            token_map[tk] = mapping
    for tk in ambiguous:
        token_map.pop(tk, None)
    return norm_map, token_map


_NORM_MAP, _TOKEN_MAP = _build_lookup_indexes()


def resolve_mapping(label: str):
    if not label:
        return None
    raw = str(label).lower().strip()
    if raw in PARAM_MAPPING:
        return PARAM_MAPPING[raw]
    norm = normalize_label(label)
    if not norm:
        return None
    if norm in PARAM_MAPPING:
        return PARAM_MAPPING[norm]
    if norm in _NORM_MAP:
        return _NORM_MAP[norm]
    for a, b in _SWAP_PHRASES:
        if a in norm:
            swapped = norm.replace(a, b)
            if swapped in _NORM_MAP:
                return _NORM_MAP[swapped]
            if swapped in PARAM_MAPPING:
                return PARAM_MAPPING[swapped]
    tk = _token_key(label)
    if tk in _TOKEN_MAP:
        return _TOKEN_MAP[tk]
    best = None
    best_ratio = 0.0
    for nk, mapping in _NORM_MAP.items():
        if abs(len(nk) - len(norm)) > max(8, int(len(norm) * 0.25)):
            continue
        ratio = SequenceMatcher(None, norm, nk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = mapping
    if best is not None and best_ratio >= FUZZY_THRESHOLD:
        return best
    return None
'''.lstrip()
)

out = SERVICES / "xbrl_mapping.py"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {out} ({out.stat().st_size} bytes)")
