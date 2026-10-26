"""What Jansky remembers about you: a plain Markdown file, one fact per line.

The file is read into the persona every time the LLM context is primed, so Jansky knows these things
in any conversation. "Remember that ..." adds a line, "forget ..." removes one, and you can edit the
file by hand. Facts are stored in your own words ("Alex said: my sister's birthday is March 3rd"), which
keeps them grammatical without rewriting, and only the newest few go into the 2048-token prompt.
"""
import re

from . import config

SEED = """# What Jansky remembers about you

One fact per line, starting with "- ". Edit freely. Jansky adds lines when told "remember ...",
and the newest ones go into its prompt.
"""

LEAD = re.compile(r"^(?:hey jansky[,.]?\s*)?(?:please\s+|okay\s+|so\s+)?(?:can you\s+)?"
                  r"(?:remember|don'?t forget|do not forget|keep in mind|note|for the record)\s*(?:that\s*|this\s*:?\s*)?", re.I)
TRAIL = re.compile(r"[,.\s]*(?:don'?t forget|remember that|okay)\W*$", re.I)


def _ensure():
    if not config.MEMORY_FILE.exists():
        config.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.MEMORY_FILE.write_text(SEED)


def facts():
    _ensure()
    return [line[2:].strip() for line in config.MEMORY_FILE.read_text().splitlines() if line.startswith("- ")]


def for_prompt():
    return "\n".join(f"- {f}" for f in facts()[-config.MEMORY_IN_PROMPT:]) or "- Nothing yet."


def to_you(fact):
    """'Alex said: my birthday is in May' -> 'your birthday is in May', for saying it back."""
    t = re.sub(r"^[^:]{1,40} said:\s*", "", fact)
    for a, b in ((r"\bI'm\b", "you're"), (r"\bI am\b", "you are"), (r"\bI've\b", "you've"), (r"\bI'll\b", "you'll"),
                 (r"\bmy\b", "your"), (r"\bmine\b", "yours"), (r"\bme\b", "you"), (r"\bI\b", "you")):
        t = re.sub(a, b, t, flags=re.I)
    return t.rstrip(".")


def add(text):
    """Store what you asked Jansky to remember. Returns the fact as stored, or None if there was nothing."""
    body = TRAIL.sub("", LEAD.sub("", text.strip())).strip(" .,!?")
    if len(body.split()) < 2:
        return None
    fact = f"{config.USER} said: {body}."
    if fact.lower() in (f.lower() for f in facts()):
        return fact
    with config.MEMORY_FILE.open("a") as f:
        f.write(f"- {fact}\n")
    return fact


def forget(text):
    """Remove the facts that share the request's key words. Returns how many were removed."""
    words = {w for w in re.findall(r"[a-z']{4,}", text.lower()) if w not in ("forget", "that", "about", "please", "jansky")}
    if not words:
        return 0
    lines = config.MEMORY_FILE.read_text().splitlines()
    keep = [l for l in lines if not (l.startswith("- ") and len(words & set(re.findall(r"[a-z']{4,}", l.lower()))) >= max(1, len(words) // 2))]
    config.MEMORY_FILE.write_text("\n".join(keep) + "\n")
    return len(lines) - len(keep)
