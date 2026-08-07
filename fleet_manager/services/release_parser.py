from __future__ import annotations
import re

def parse_breaking_sections(text: str | None) -> tuple[bool, str | None]:
    if not text: return False, None
    lines = text.splitlines()
    sections=[]
    pat = re.compile(r'(breaking changes?|backward.?incompatible|action required|migration required|deprecated|removed|important|upgrade notes?)', re.I)
    for i,line in enumerate(lines):
        if pat.search(line): sections.extend(lines[i:i+20])
    out='\n'.join(sections).strip()
    return bool(out), out[:4000] if out else None
