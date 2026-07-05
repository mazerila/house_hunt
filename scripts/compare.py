#!/usr/bin/env python3
"""Compare two French DPE (ADEME 3CL) XML files and report every mismatch.

DPE XML files store the data behind an energy-performance diagnostic. Two DPEs
for the same dwelling can disagree (different inputs -> different energy class),
and the interesting question is always *exactly which fields changed*. This
script flattens both files into path -> value maps and reports added, removed,
and changed fields.

The hard part is collections (walls, windows, floors, ...): each item carries a
file-specific GUID `reference`, so items cannot be matched by position or by
reference across files. Instead items are matched by a stable key derived from
the human-readable `description` label prefix (e.g. "Mur  1 Ouest"), which is
preserved even when the rest of the description changes.

Usage:
    scripts/compare_dpe.py OLD.xml NEW.xml       # explicit pair
    scripts/compare_dpe.py a.xml b.xml --format json
    scripts/compare_dpe.py a.xml b.xml --include-refs --tol 1e-6

Exit status is 0 when the files are identical (modulo ignored fields), 1 when
mismatches are found, 2 on error -- handy for scripting.
"""

from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _local(elem_or_tag) -> str:
    """Strip the XML namespace; accepts an Element or a raw tag string."""
    tag = elem_or_tag.tag if isinstance(elem_or_tag, ET.Element) else elem_or_tag
    return tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element) -> str:
    return (elem.text or "").strip()


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


# Fields that are file-specific GUIDs / cross-references. They change between
# every export and carry no analytical meaning, so they are ignored for value
# comparison by default (use --include-refs to see them).
_REF_FIELDS = ("reference",)


def _is_ref_field(tag: str) -> bool:
    return tag == "reference" or tag.startswith("reference_")


# ---------------------------------------------------------------------------
# Stable keys for collection items
# ---------------------------------------------------------------------------

# Enum / id fields used as a fallback key for collection items that have no
# description (in priority order).
_KEY_FIELDS = (
    "enum_type_energie_id",
    "enum_num_pack_travaux_id",
    "enum_lot_travaux_id",
    "enum_categorie_fiche_technique_id",
    "enum_categorie_descriptif_simplifie_id",
    "enum_picto_geste_entretien_id",
    "enum_type_justificatif_id",
)


def _shallow_description(elem: ET.Element) -> str:
    """First `description` text found at depth <= 2 (direct child, or under a
    direct child such as `donnee_entree`). Envelope items keep their description
    one level down, so a direct-children-only lookup would miss it."""
    for c in elem:
        if _local(c) == "description" and _text(c):
            return _text(c)
    for c in elem:
        for gc in c:
            if _local(gc) == "description" and _text(gc):
                return _text(gc)
    return ""


def _item_key(elem: ET.Element) -> str | None:
    """Best-effort *stable* identifier for one item of a collection.

    Returns None when nothing identifying is found (caller falls back to a
    positional index). The result must be stable across the two files for the
    same logical item, so GUID references are never used.
    """
    desc = _shallow_description(elem)
    if desc:
        # The label before " - " is the stable part ("Mur  1 Ouest"); the rest
        # of the description encodes properties that legitimately change.
        return _norm_ws(desc.split(" - ", 1)[0])

    direct = {}
    for c in elem:
        direct.setdefault(_local(c), c)  # first occurrence wins
    for kf in _KEY_FIELDS:
        c = direct.get(kf)
        if c is not None and _text(c):
            return f"{kf}={_text(c)}"

    # Generic fallback: combine all direct scalar *_id fields (minus refs).
    parts = []
    for c in elem:
        tag = _local(c)
        if list(c) or _is_ref_field(tag):
            continue
        if tag.endswith("_id") and _text(c):
            parts.append(f"{tag}={_text(c)}")
    return ";".join(parts) if parts else None


def _keyed_children(items: list[ET.Element]) -> list[tuple[str, ET.Element]]:
    """Assign a unique-within-this-collection key to each repeated item.

    Stable keys are kept verbatim; collisions get an occurrence suffix (#2, #3)
    in document order so that two files with the same item count still line up.
    """
    raw = [(_item_key(it) or f"#{i + 1}", it) for i, it in enumerate(items)]
    seen: dict[str, int] = {}
    out = []
    for key, it in raw:
        n = seen.get(key, 0) + 1
        seen[key] = n
        out.append((key if n == 1 else f"{key}#{n}", it))
    return out


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

# Collections that merely re-render data already compared elsewhere (the
# printable "fiche technique" sheets). Their items are keyed only by a category
# enum and cannot be aligned across files, so they are skipped unless --full.
_DERIVED_COLLECTIONS = ("fiche_technique_collection",)


def flatten(elem: ET.Element, include_refs: bool = False,
            full: bool = False) -> dict[str, str]:
    """Flatten an element subtree into {path: value}.

    Repeated siblings (collections) get a `tag[key]` path segment; everything
    else uses plain `tag` segments.
    """
    out: dict[str, str] = {}

    def recurse(node: ET.Element, path: str) -> None:
        children = list(node)
        if not children:
            tag = _local(node)
            if not include_refs and _is_ref_field(tag):
                return
            out[path] = _text(node)
            return

        # group children by tag to detect collections
        groups: dict[str, list[ET.Element]] = {}
        for c in children:
            groups.setdefault(_local(c), []).append(c)

        for tag, items in groups.items():
            if not full and tag in _DERIVED_COLLECTIONS:
                continue
            if len(items) == 1:
                recurse(items[0], f"{path}/{tag}")
            else:
                for key, it in _keyed_children(items):
                    recurse(it, f"{path}/{tag}[{key}]")

    recurse(elem, _local(elem))
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _values_equal(a: str, b: str, tol: float) -> bool:
    if a == b:
        return True
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return math.isclose(fa, fb, rel_tol=tol, abs_tol=tol)


class Diff:
    def __init__(self) -> None:
        self.changed: list[tuple[str, str, str]] = []   # path, old, new
        self.removed: list[tuple[str, str]] = []        # path, old (only in A)
        self.added: list[tuple[str, str]] = []          # path, new (only in B)

    @property
    def total(self) -> int:
        return len(self.changed) + len(self.removed) + len(self.added)


def compare(a: dict[str, str], b: dict[str, str], tol: float) -> Diff:
    d = Diff()
    for path in sorted(set(a) | set(b)):
        if path not in b:
            d.removed.append((path, a[path]))
        elif path not in a:
            d.added.append((path, b[path]))
        elif not _values_equal(a[path], b[path], tol):
            d.changed.append((path, a[path], b[path]))
    return d


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class C:
    """ANSI colors, disabled when not a tty or --no-color."""
    enabled = True

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s

    @classmethod
    def bold(cls, s): return cls._w("1", s)
    @classmethod
    def red(cls, s): return cls._w("31", s)
    @classmethod
    def green(cls, s): return cls._w("32", s)
    @classmethod
    def yellow(cls, s): return cls._w("33", s)
    @classmethod
    def cyan(cls, s): return cls._w("36", s)
    @classmethod
    def dim(cls, s): return cls._w("2", s)


# Headline fields worth surfacing first (matched by path suffix).
_HEADLINE = [
    ("numero_dpe", "DPE number"),
    ("statut", "Status"),
    ("administratif/date_etablissement_dpe", "Established"),
    ("caracteristique_generale/annee_construction", "Build year"),
    ("ep_conso/ep_conso_5_usages_m2", "Primary energy (kWhEP/m2/an)"),
    ("ep_conso/classe_bilan_dpe", "Energy class"),
    ("emission_ges/emission_ges_5_usages_m2", "GES (kgCO2/m2/an)"),
    ("emission_ges/classe_emission_ges", "GES class"),
    ("deperdition/deperdition_enveloppe", "Heat loss envelope (W/K)"),
    ("qualite_isolation/ubat", "Ubat (W/m2.K)"),
]


def _find_by_suffix(d: dict[str, str], suffix: str) -> str | None:
    for path, val in d.items():
        if path.endswith(suffix):
            return val
    return None


def _section(path: str) -> str:
    # group by the first two path segments for readability
    parts = path.split("/")
    return "/".join(parts[1:3]) if len(parts) > 2 else parts[0]


def render_text(a_name: str, b_name: str, a: dict, b: dict, diff: Diff,
                full: bool) -> str:
    lines: list[str] = []
    lines.append(C.bold("DPE comparison"))
    lines.append(f"  {C.dim('OLD (A):')} {a_name}")
    lines.append(f"  {C.dim('NEW (B):')} {b_name}")
    lines.append("")

    # Headline table -------------------------------------------------------
    lines.append(C.bold("Headline figures"))
    for suffix, label in _HEADLINE:
        va = _find_by_suffix(a, suffix)
        vb = _find_by_suffix(b, suffix)
        if va is None and vb is None:
            continue
        same = va == vb
        arrow = "=" if same else "->"
        va_s = va if va is not None else "--"
        vb_s = vb if vb is not None else "--"
        line = f"  {label:<32} {va_s:>10} {arrow} {vb_s:<10}"
        lines.append(line if same else C.yellow(line))
    lines.append("")

    # Summary --------------------------------------------------------------
    lines.append(C.bold("Summary"))
    lines.append(
        f"  {C.yellow(str(len(diff.changed)))} changed, "
        f"{C.red(str(len(diff.removed)))} only in OLD, "
        f"{C.green(str(len(diff.added)))} only in NEW "
        f"({diff.total} mismatches total)"
    )
    if not full:
        lines.append(C.dim(
            "  (derived 'fiche technique' sheets excluded — pass --full to include)"
        ))
    lines.append("")

    if diff.total == 0:
        lines.append(C.green("No mismatches. The two DPEs are equivalent."))
        return "\n".join(lines)

    # Grouped detail -------------------------------------------------------
    def group(entries):
        g: dict[str, list] = {}
        for e in entries:
            g.setdefault(_section(e[0]), []).append(e)
        return g

    if diff.changed:
        lines.append(C.bold(C.yellow(f"Changed fields ({len(diff.changed)})")))
        for sec, entries in group(diff.changed).items():
            lines.append(f"  {C.cyan(sec)}")
            for path, old, new in entries:
                leaf = path.split("/", 2)[-1] if path.count("/") >= 2 else path
                lines.append(f"    {leaf}")
                lines.append(f"        {C.red(old or '∅')}  {C.dim('->')}  {C.green(new or '∅')}")
        lines.append("")

    if diff.removed:
        lines.append(C.bold(C.red(f"Only in OLD ({len(diff.removed)})")))
        for sec, entries in group(diff.removed).items():
            lines.append(f"  {C.cyan(sec)}")
            for path, old in entries:
                leaf = path.split("/", 2)[-1] if path.count("/") >= 2 else path
                lines.append(f"    {leaf} = {C.red(old or '∅')}")
        lines.append("")

    if diff.added:
        lines.append(C.bold(C.green(f"Only in NEW ({len(diff.added)})")))
        for sec, entries in group(diff.added).items():
            lines.append(f"  {C.cyan(sec)}")
            for path, new in entries:
                leaf = path.split("/", 2)[-1] if path.count("/") >= 2 else path
                lines.append(f"    {leaf} = {C.green(new or '∅')}")
        lines.append("")

    return "\n".join(lines).rstrip()


def render_json(a_name: str, b_name: str, diff: Diff) -> str:
    import json
    payload = {
        "old": a_name,
        "new": b_name,
        "summary": {
            "changed": len(diff.changed),
            "only_in_old": len(diff.removed),
            "only_in_new": len(diff.added),
            "total": diff.total,
        },
        "changed": [{"path": p, "old": o, "new": n} for p, o, n in diff.changed],
        "only_in_old": [{"path": p, "value": v} for p, v in diff.removed],
        "only_in_new": [{"path": p, "value": v} for p, v in diff.added],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Programmatic API (importable helpers)
# ---------------------------------------------------------------------------

def compare_payload(root_a: ET.Element, root_b: ET.Element, *,
                    a_name: str = "old", b_name: str = "new",
                    tol: float = 1e-9, include_refs: bool = False,
                    full: bool = False) -> dict:
    """Compare two parsed DPE roots and return a JSON-ready dict.

    Same shape as render_json() plus a `headline` table (the _HEADLINE fields
    side by side) so a caller can render the G->E summary without re-parsing.
    """
    a = flatten(root_a, include_refs=include_refs, full=full)
    b = flatten(root_b, include_refs=include_refs, full=full)
    diff = compare(a, b, tol=tol)

    headline = []
    for suffix, label in _HEADLINE:
        va = _find_by_suffix(a, suffix)
        vb = _find_by_suffix(b, suffix)
        if va is None and vb is None:
            continue
        headline.append({"label": label, "old": va, "new": vb,
                         "changed": va != vb})

    return {
        "old": a_name,
        "new": b_name,
        "summary": {
            "changed": len(diff.changed),
            "only_in_old": len(diff.removed),
            "only_in_new": len(diff.added),
            "total": diff.total,
        },
        "headline": headline,
        "changed": [{"path": p, "old": o, "new": n} for p, o, n in diff.changed],
        "only_in_old": [{"path": p, "value": v} for p, v in diff.removed],
        "only_in_new": [{"path": p, "value": v} for p, v in diff.added],
    }


def compare_bytes(data_a: bytes, data_b: bytes, **kw) -> dict:
    """compare_payload from two raw XML byte strings."""
    return compare_payload(ET.fromstring(data_a), ET.fromstring(data_b), **kw)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compare two DPE XML files and report all mismatches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("files", nargs=2, metavar="XML",
                   help="two DPE XML files (OLD NEW)")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--tol", type=float, default=1e-9,
                   help="relative/absolute tolerance for numeric values "
                        "(default 1e-9, i.e. effectively exact)")
    p.add_argument("--include-refs", action="store_true",
                   help="include GUID reference fields (noisy; off by default)")
    p.add_argument("--full", action="store_true",
                   help="also compare derived 'fiche technique' sheets "
                        f"({', '.join(_DERIVED_COLLECTIONS)}); off by default "
                        "because they only re-render data compared elsewhere")
    p.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = p.parse_args(argv)

    files = args.files

    C.enabled = (not args.no_color) and sys.stdout.isatty() and args.format == "text"

    try:
        roots = [ET.parse(f).getroot() for f in files]
    except (ET.ParseError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    a = flatten(roots[0], include_refs=args.include_refs, full=args.full)
    b = flatten(roots[1], include_refs=args.include_refs, full=args.full)
    diff = compare(a, b, tol=args.tol)

    a_name, b_name = files[0], files[1]
    if args.format == "json":
        print(render_json(a_name, b_name, diff))
    else:
        print(render_text(a_name, b_name, a, b, diff, full=args.full))

    return 1 if diff.total else 0


if __name__ == "__main__":
    sys.exit(main())
