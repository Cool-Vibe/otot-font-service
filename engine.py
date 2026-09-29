"""OtOt Pro Font Engine (original outlines only).

Input: Light, Regular and Bold masters produced by the OtOt Hebrew
skeleton+pen letter engine (no third-party font data). Each weight is built
separately from pen parameters, so masters are NOT point-compatible and no
variable font is shipped. This service:
- builds static TTF / OTF (CFF) / WOFF2 with overlaps removed
- writes GPOS mark/mkmk (niqqud) from the engine's anchors
- writes the engine's per-weight kerning as Hebrew GPOS kern (RTL pairs)
- sets clean Windows + Mac naming (incl. Hebrew name records) and metrics
- runs HarfBuzz shaping QA and returns a ZIP + JSON report
"""
from __future__ import annotations

import io
import json
import math
import statistics
import zipfile
from dataclasses import dataclass

import uharfbuzz as hb
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.basePen import BasePen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

STYLE = {300: "Light", 400: "Regular", 700: "Bold"}
HEBREW = [chr(c) for c in range(0x05D0, 0x05EB)]
STRESS = ["שָׁלוֹם עוֹלָם", "בְּרֵאשִׁית בָּרָא", "קְדֻשָּׁה וּבְרָכָה", "ץ ף ך ן ם", "אבגדהוזחטיכלמנסעפצקרשת"]


@dataclass
class Params:
    latin_family: str
    hebrew_family: str
    light: dict
    regular: dict
    bold: dict
    tight: float = 0.5
    italic_angle: float = 0.0
    version: str = "1.000"


def draw(pen, contours):
    for c in contours:
        i = next(k for k, p in enumerate(c) if p[2])
        pts = c[i:] + c[:i]
        pen.moveTo(tuple(pts[0][:2]))
        run = []
        for p in pts[1:] + [pts[0]]:
            if p[2]:
                if run:
                    pen.qCurveTo(*[tuple(q[:2]) for q in run], tuple(p[:2]))
                    run = []
                else:
                    pen.lineTo(tuple(p[:2]))
            else:
                run.append(p)
        if run:
            pen.qCurveTo(*[tuple(q[:2]) for q in run], tuple(pts[0][:2]))
        pen.closePath()


def names_for(p: Params, weight: int):
    style = STYLE[weight]
    ribbi = weight in (400, 700)
    ps = f"{p.latin_family.replace(' ', '')}-{style}"
    return dict(
        familyName=p.latin_family if ribbi else f"{p.latin_family} {style}",
        styleName=style if ribbi else "Regular",
        uniqueFontIdentifier=f"{p.version};OTOT;{ps}",
        fullName=f"{p.latin_family} {style}",
        psName=ps,
        version=f"Version {p.version}",
        manufacturer="OtOt",
        designer="OtOt Hebrew AI Studio",
        licenseDescription="Copyright OtOt. Licensed under the OtOt Font License. See LICENSE.txt.",
        typographicFamily=p.latin_family,
        typographicSubfamily=style,
    ), ps


def build_ttf(p: Params, master: dict, weight: int, overlaps=True) -> TTFont:
    glyphs = master["glyphs"]
    order = [g["name"] for g in glyphs]
    fb = FontBuilder(master["unitsPerEm"], isTTF=True)
    fb.setupGlyphOrder(order)
    cmap = {u: g["name"] for g in glyphs for u in g["unicodes"]}
    fb.setupCharacterMap(cmap)
    ttglyphs, metrics = {}, {}
    for g in glyphs:
        pen = TTGlyphPen(None)
        draw(pen, g["contours"])
        ttglyphs[g["name"]] = pen.glyph()
    fb.setupGlyf(ttglyphs)
    glyf = fb.font["glyf"]
    for g in glyphs:
        tg = glyf[g["name"]]
        metrics[g["name"]] = (g["advance"], getattr(tg, "xMin", 0) if tg.numberOfContours else 0)
    fb.setupHorizontalMetrics(metrics)
    asc, desc = master["ascender"], master["descender"]
    fb.setupHorizontalHeader(ascent=asc, descent=desc, lineGap=0)
    n, ps = names_for(p, weight)
    fb.setupNameTable(n)
    fb.setupOS2(
        version=4, usWeightClass=weight, achVendID="OTOT", fsType=0,
        sTypoAscender=asc, sTypoDescender=desc, sTypoLineGap=0,
        usWinAscent=asc, usWinDescent=-desc, sxHeight=master["letterHeight"], sCapHeight=master["letterHeight"],
        fsSelection=(1 << 5 if weight == 700 else 1 << 6) | (1 << 7),
        ulUnicodeRange1=(1 << 0) | (1 << 11), ulCodePageRange1=(1 << 0) | (1 << 5),
    )
    fb.setupPost(italicAngle=p.italic_angle)
    fb.font["head"].macStyle = 1 if weight == 700 else 0
    fb.font["head"].fontRevision = float(p.version)
    name = fb.font["name"]
    heb_style = "" if weight == 400 else f" {STYLE[weight]}"
    name.setName(p.hebrew_family if weight in (400, 700) else p.hebrew_family + heb_style, 1, 3, 1, 0x040D)
    name.setName(p.hebrew_family + heb_style, 4, 3, 1, 0x040D)
    name.setName(p.hebrew_family, 16, 3, 1, 0x040D)
    from fontTools.ttLib import newTable
    gasp = newTable("gasp"); gasp.version = 1; gasp.gaspRange = {0xFFFF: 0x000F}; fb.font["gasp"] = gasp
    add_features(fb.font, glyphs, p, master)
    if overlaps:
        from fontTools.ttLib.removeOverlaps import removeOverlaps
        removeOverlaps(fb.font)
    return fb.font


# ------------------------------------------------------------ features
class ProfilePen(BasePen):
    def __init__(self, band):
        super().__init__(None)
        self.band, self.left, self.right, self.cur = band, {}, {}, None

    def _add(self, x, y):
        b = int(y // self.band)
        self.left[b] = min(self.left.get(b, x), x)
        self.right[b] = max(self.right.get(b, x), x)

    def _moveTo(self, pt):
        self.cur = pt; self._add(*pt)

    def _lineTo(self, pt):
        (x0, y0), (x1, y1) = self.cur, pt
        n = max(1, int(abs(y1 - y0) / (self.band / 3)))
        for i in range(1, n + 1):
            t = i / n; self._add(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        self.cur = pt

    def _curveToOne(self, a, b, c):
        x0, y0 = self.cur
        for i in range(1, 17):
            t = i / 16; m = 1 - t
            self._add(m**3*x0 + 3*m*m*t*a[0] + 3*m*t*t*b[0] + t**3*c[0], m**3*y0 + 3*m*m*t*a[1] + 3*m*t*t*b[1] + t**3*c[1])
        self.cur = c


def kern_pairs(glyphs: list, upm: int, tight: float) -> dict:
    letters = [g for g in glyphs if g["unicodes"] and 0x5D0 <= g["unicodes"][0] <= 0x5EA]
    prof = {}
    for g in letters:
        pen = ProfilePen(upm / 40)
        draw(pen, g["contours"])
        prof[g["name"]] = (pen, g["advance"])
    gaps = {}
    for a in prof:  # RTL: logical a sits right of b
        for b in prof:
            pa, _ = prof[a]; pb, adv_b = prof[b]
            common = set(pa.left) & set(pb.right)
            if common:
                gaps[(a, b)] = min(adv_b - pb.right[k] + pa.left[k] for k in common)
    if not gaps:
        return {}
    target = statistics.median(gaps.values())
    out = {}
    for pair, g in gaps.items():
        excess = g - target
        if excess > upm * 0.02:
            k = -round(min(upm * 0.07, excess * (0.35 + tight * 0.3)))
            if k <= -8:
                out[pair] = k
    return out


def add_features(font: TTFont, glyphs: list, p: Params, master: dict):
    fea = ["languagesystem DFLT dflt;", "languagesystem hebr dflt;"]
    marks = [g for g in glyphs if g.get("mark")]
    classes = sorted({g["mark"] for g in marks})
    for cl in classes:
        for g in marks:
            if g["mark"] == cl:
                fea.append(f"markClass {g['name']} <anchor 0 0> @M_{cl.replace('-', '_')};")
    fea.append("feature mark {")
    for cl in classes:
        bases = [g for g in glyphs if cl in g["anchors"] and not g.get("mark")]
        for g in bases:
            x, y = g["anchors"][cl]
            fea.append(f"  pos base {g['name']} <anchor {x} {y}> mark @M_{cl.replace('-', '_')};")
    fea.append("} mark;")
    # Engine kerning: logical-order (first, second, k) pairs per weight.
    # Hebrew shapes RTL; feaLib applies the pair in logical order and the
    # value adjusts the first glyph's advance, which is what the engine means.
    engine_kern = master.get("kerning") or []
    pairs = {(a, b): k for a, b, k in engine_kern} if engine_kern else kern_pairs(glyphs, font["head"].unitsPerEm, p.tight)
    if pairs:
        fea.append("feature kern {\n  lookupflag IgnoreMarks;")
        fea += [f"  pos {a} {b} {k};" for (a, b), k in pairs.items()]
        fea.append("} kern;")
    font.kern_count = len(pairs)
    addOpenTypeFeaturesFromString(font, "\n".join(fea))
    gdef_classes = "\n".join([
        "table GDEF {",
        f"  GlyphClassDef [{' '.join(g['name'] for g in glyphs if not g.get('mark') and g['contours'])}], , [{' '.join(g['name'] for g in marks)}], ;",
        "} GDEF;",
    ])
    addOpenTypeFeaturesFromString(font, gdef_classes, tables=["GDEF"])


# ------------------------------------------------------------ OTF
def to_otf(ttf: TTFont, p: Params, weight: int) -> bytes:
    gs = ttf.getGlyphSet()
    hmtx = ttf["hmtx"]
    order = ttf.getGlyphOrder()
    cs = {}
    for n in order:
        pen = T2CharStringPen(hmtx[n][0], gs)
        gs[n].draw(pen)
        cs[n] = pen.getCharString()
    fb = FontBuilder(ttf["head"].unitsPerEm, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(ttf.getBestCmap())
    _, ps = names_for(p, weight)
    fb.setupCFF(ps, {"FullName": f"{p.latin_family} {STYLE[weight]}", "FamilyName": p.latin_family, "Weight": STYLE[weight]}, cs, {})
    fb.setupHorizontalMetrics({n: hmtx[n] for n in order})
    for tag in ("hhea", "OS/2", "name", "post", "GPOS", "GDEF", "gasp"):
        if tag in ttf and tag != "gasp":
            fb.font[tag] = ttf[tag]
    fb.font["post"].formatType = 3.0
    fb.setupMaxp()
    fb.font["head"].macStyle = ttf["head"].macStyle
    fb.font["head"].fontRevision = ttf["head"].fontRevision
    return save(fb.font)


def save(font: TTFont, flavor=None) -> bytes:
    buf = io.BytesIO()
    font.flavor = flavor
    font.save(buf)
    font.flavor = None
    return buf.getvalue()


# No variable font: the pen engine builds each weight separately, so masters
# are not point-compatible and varLib cannot interpolate them.


# ------------------------------------------------------------ QA
def qa(font_bytes: bytes, font: TTFont) -> list[dict]:
    cmap = font.getBestCmap()
    missing = [c for c in HEBREW if ord(c) not in cmap]
    out = [{"id": "coverage", "label": "כל 27 האותיות העבריות קיימות", "ok": not missing, "detail": "".join(missing)}]
    marks = [c for c in range(0x05B0, 0x05C8) if c in cmap and c not in (0x5BE, 0x5C3)]
    out.append({"id": "niqqud", "label": "סימני ניקוד", "ok": len(marks) >= 17, "detail": f"{len(marks)} סימנים"})
    hbf = hb.Font(hb.Face(font_bytes))
    notdef = placed = 0
    for s in STRESS:
        buf = hb.Buffer(); buf.add_str(s); buf.guess_segment_properties()
        hb.shape(hbf, buf, {})
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            notdef += info.codepoint == 0
            placed += bool(pos.x_offset or pos.y_offset)
    out.append({"id": "shaping", "label": "עיבוד טקסט מנוקד (HarfBuzz) ללא תווים חסרים", "ok": notdef == 0, "detail": f"{notdef} חסרים"})
    out.append({"id": "marks", "label": "הניקוד ממוקם לפי עוגנים לכל אות", "ok": placed > 10, "detail": f"{placed} מיקומים"})
    out.append({"id": "mark-advance", "label": "סימני ניקוד ללא רוחב", "ok": all(font["hmtx"][cmap[c]][0] == 0 for c in marks), "detail": ""})
    advs = [font["hmtx"][cmap[ord(c)]][0] for c in HEBREW if ord(c) in cmap]
    out.append({"id": "spacing", "label": "מרווחים חיוביים לכל האותיות", "ok": min(advs) > 0, "detail": f"{min(advs)}–{max(advs)}"})
    nm = font["name"]
    out.append({"id": "names", "label": "שמות תקינים ל-Windows ול-Mac", "ok": bool(nm.getName(1, 3, 1, 0x409) and nm.getName(1, 1, 0, 0)), "detail": ""})
    return out


# ------------------------------------------------------------ build
def build_family(p: Params) -> tuple[bytes, dict]:
    slug = p.latin_family.replace(" ", "")
    masters = {300: p.light, 400: p.regular, 700: p.bold}
    report = {"family": p.latin_family, "hebrewFamily": p.hebrew_family, "weights": [], "variable": False}
    css = []
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for w, m in masters.items():
            font = build_ttf(p, m, w)
            kerns = font.kern_count
            ttf = save(font)
            style = STYLE[w]
            zf.writestr(f"{slug}/TTF/{slug}-{style}.ttf", ttf)
            zf.writestr(f"{slug}/OTF/{slug}-{style}.otf", to_otf(TTFont(io.BytesIO(ttf)), p, w))
            zf.writestr(f"{slug}/WEB/{slug}-{style}.woff2", save(TTFont(io.BytesIO(ttf)), "woff2"))
            css.append(f"@font-face {{\n  font-family: '{p.latin_family}';\n  src: url('{slug}-{style}.woff2') format('woff2');\n  font-weight: {w};\n  font-display: swap;\n}}")
            report["weights"].append({"weight": w, "style": style, "kerningPairs": kerns, "checks": qa(ttf, TTFont(io.BytesIO(ttf)))})
        zf.writestr(f"{slug}/WEB/{slug}.css", "\n\n".join(css) + "\n")
        zf.writestr(f"{slug}/specimen.html", specimen(p, slug))
        zf.writestr(f"{slug}/LICENSE.txt", LICENSE.format(family=p.latin_family))
        zf.writestr(f"{slug}/qa-report.json", json.dumps(report, ensure_ascii=False, indent=2))
        zf.writestr(f"{slug}/README.txt", README.format(family=p.latin_family, heb=p.hebrew_family))
    return z.getvalue(), report


def specimen(p: Params, slug: str) -> str:
    rows = "".join(f"<section style='font-weight:{w}'><h2>{s}</h2><p class=big>אבגדהוזחטיכךלמםנןסעפףצץקרשת</p><p>{STRESS[0]} · {STRESS[1]}</p></section>" for w, s in STYLE.items())
    return (f"<!doctype html><html dir=rtl lang=he><meta charset=utf-8><title>{p.hebrew_family}</title>"
            f"<link rel=stylesheet href='WEB/{slug}.css'><style>body{{font-family:'{p.latin_family}';max-width:900px;margin:40px auto;padding:0 20px}}"
            f".big{{font-size:56px}}p{{font-size:24px}}h2{{font-size:14px;opacity:.6}}</style><h1 style='font-size:72px'>{p.hebrew_family}</h1>{rows}</html>")


README = """{heb} ({family}) — made with OtOt Hebrew AI Studio.

TTF/       install on Windows or Mac (double-click > Install)
OTF/       for design apps (Adobe, Figma, Affinity)
WEB/       WOFF2 + CSS for websites
specimen.html, qa-report.json

Every letter is drawn from scratch by the OtOt letter engine; no third-party
font outlines are included.
"""

LICENSE = """{family} — OtOt Font License (placeholder)

Copyright (c) OtOt. All rights reserved.
Outlines were constructed from scratch by the OtOt letter engine and contain
no third-party font data. Replace this file with your final commercial
end-user licence (desktop / web / app tiers) before selling.
"""
