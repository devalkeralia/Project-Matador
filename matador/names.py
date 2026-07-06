import re
import unicodedata

# Normalized-full-name -> canonical-key overrides for known Kalshi/Sackmann mismatches
# (e.g. multi-word surnames a naive "first last" split gets wrong). Keys are the output
# of normalize(); add entries here as real mismatches turn up, not speculatively.
ALIASES: dict[str, str] = {
    "juan martin del potro": "del_potro_j",
}

_NON_NAME_CHARS = re.compile(r"[^a-z0-9\s'\-,]")
_WHITESPACE = re.compile(r"\s+")
_TITLE_SEPARATOR = re.compile(r"\s+v(?:s)?\.?\s+", re.IGNORECASE)
_TRAILING_INITIAL = re.compile(r"_[a-z]$")


def normalize(raw: str) -> str:
    """Casefold, strip accents, collapse whitespace. Keeps letters/digits/spaces/hyphens/apostrophes."""
    decomposed = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.casefold()
    letters_only = _NON_NAME_CHARS.sub(" ", lowered)
    return _WHITESPACE.sub(" ", letters_only).strip()


def _split_name(normalized: str) -> tuple[str, str]:
    """Return (first_name, surname) from a normalized 'first last' or 'last, first' string."""
    if "," in normalized:
        last, _, first = normalized.partition(",")
        return first.strip(), last.strip()

    parts = normalized.split(" ")
    if len(parts) == 1:
        return "", parts[0]
    first, *rest = parts
    return first, " ".join(rest)


def _surname_to_key(surname: str) -> str:
    return surname.replace(" ", "_").replace("-", "_").replace("'", "")


def canonical_key(raw: str) -> str:
    """The single join key shared by Kalshi market yes_sub_title/no_sub_title (full names)
    and the Sackmann Elo lookup.

    Surname + first initial (e.g. "Jannik Sinner" -> "sinner_j"). Same-surname+initial
    collisions are disambiguated elsewhere (by event date); true mismatches go in ALIASES.
    """
    normalized = normalize(raw)
    if normalized in ALIASES:
        return ALIASES[normalized]

    first, surname = _split_name(normalized)
    surname_part = _surname_to_key(surname)
    initial = first[0] if first else ""
    return f"{surname_part}_{initial}" if initial else surname_part


def surname_only_key(raw: str) -> str:
    """Treat the whole string as a surname, with no first/last splitting.

    Kalshi's event `title` field ("de Minaur vs Svajda", "Davidovich Fokina vs Fucsovics")
    is always surname-only, including multi-word surnames -- splitting it like a "first
    last" name (canonical_key) would wrongly peel off "de"/"Davidovich" as a first name.
    """
    return _surname_to_key(normalize(raw))


def surname_key(key: str) -> str:
    """Strip a trailing '_<initial>' from a canonical_key, to compare against a
    surname_only_key (e.g. canonical_key("Alex de Minaur") -> "de_minaur_a" -> "de_minaur")."""
    return _TRAILING_INITIAL.sub("", key)


def keys_from_title(text: str) -> tuple[str, str] | None:
    """Parse a Kalshi-style 'Surname vs Surname' / 'Surname v Surname' event title into
    two surname-only keys (comparable against canonical_key(...) via surname_key()), or None."""
    parts = _TITLE_SEPARATOR.split(text.strip(), maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return surname_only_key(parts[0]), surname_only_key(parts[1])
