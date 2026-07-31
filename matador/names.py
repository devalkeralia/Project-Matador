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


# Lowercase nobiliary/patronymic particles that belong TO the surname ("de Jong", "Van De Zandschulp",
# "El Aouni", "Del Puerto"). Absorbed by walking backwards, so multi-particle names work.
_SURNAME_PARTICLES = frozenset({
    "de", "del", "della", "der", "den", "di", "da", "das", "dos", "du", "la", "le", "el", "al",
    "van", "von", "ter", "ten", "bin", "ben", "abu", "mc", "st",
})


def display_name(full_name: str, distinct_from: str | None = None) -> str:
    """Broadcast-style short form: 'T. Fritz', 'J. De Jong', 'J. Prado Angelo'.

    DISPLAY ONLY -- never use this for matching or as a key (that is canonical_key / surname_key). It is
    a heuristic over a name string and is allowed to be imperfect: every message using it also shows the
    full name, so a wrong short form cannot mislead about WHO.

    The leading initial is not decoration -- it separates same-surname players a betting message must
    not conflate ("J. Cerundolo" vs "F. Cerundolo"). It cannot separate same-INITIAL ones on its own:
    Xinyu Wang and Xiyu Wang both shorten to "X. Wang", and that pair caused a real wrong-player bug
    here. Pass `distinct_from` (the other player in the same match) and the short form is abandoned for
    the FULL name whenever the two would collide -- so a Wang-vs-Wang fixture never renders two
    identical labels. Even so this must stay a display helper and never become an identifier.

    Surname = trailing PARTICLES absorbed backwards, else the last two tokens of a 3+ token name (the
    broadcast convention: "Prado Angelo", "Davidovich Fokina"), else the last token.

    Deliberately NO given-name exception list. The 3-token class genuinely splits both ways in our data
    -- "Bautista Agut" is a double surname, "Juan Manuel Cerundolo" a double given name -- and
    classifying it needs ~90 entries maintained by hand for a cosmetic string. So "T. Martin Etcheverry"
    is accepted: with the initial occupying the given-name slot it reads as a long surname rather than
    as a first name, which is the failure mode that actually misleads.

    KNOWN LIMITATION: surname-FIRST names are wrong -- "Zheng Qinwen" yields "Z. Qinwen" instead of
    "Zheng". Same monitored gap as the resolution path (DESIGN-DECISIONS); it needs a per-player
    override, not a positional rule.
    """
    short = _short_form(full_name)
    # A collision is worse than verbosity: two identical labels in one message invite exactly the
    # wrong-player confusion this is meant to prevent, so give up the abbreviation entirely.
    if distinct_from and short and short == _short_form(distinct_from):
        return full_name
    return short


def _short_form(full_name: str) -> str:
    tokens = (full_name or "").split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    i = len(tokens) - 1
    while i > 1 and tokens[i - 1].lower() in _SURNAME_PARTICLES:
        i -= 1
    surname = " ".join(tokens[i:]) if i < len(tokens) - 1 else (
        " ".join(tokens[-2:]) if len(tokens) >= 3 else tokens[-1])
    return f"{tokens[0][0].upper()}. {surname}"
