from matador.names import canonical_key, keys_from_title, normalize, surname_key, surname_only_key


def test_normalize_strips_accents_and_case():
    assert normalize("Félix Auger-Aliassime") == "felix auger-aliassime"
    assert normalize("  Roger   Federer ") == "roger federer"
    assert normalize("O'Connell") == "o'connell"


def test_canonical_key_first_last():
    assert canonical_key("Jannik Sinner") == "sinner_j"
    assert canonical_key("Daniil Medvedev") == "medvedev_d"


def test_canonical_key_last_comma_first():
    assert canonical_key("Sinner, Jannik") == "sinner_j"


def test_canonical_key_initial_dot_last():
    assert canonical_key("J. Sinner") == "sinner_j"


def test_canonical_key_is_case_and_accent_insensitive():
    assert canonical_key("jannik sinner") == canonical_key("JANNIK SINNER")
    assert canonical_key("Félix Auger-Aliassime") == canonical_key("Felix Auger-Aliassime")


def test_canonical_key_hyphenated_surname():
    assert canonical_key("Felix Auger-Aliassime") == "auger_aliassime_f"


def test_canonical_key_apostrophe_surname():
    assert canonical_key("Christopher O'Connell") == "oconnell_c"


def test_canonical_key_surname_only_has_no_initial():
    assert canonical_key("Sinner") == "sinner"


def test_canonical_key_alias_override():
    assert canonical_key("Juan Martin del Potro") == "del_potro_j"
    assert canonical_key("juan martin del potro") == "del_potro_j"


def test_canonical_key_same_surname_and_initial_collide_by_design():
    # Disambiguating same-surname/initial players is the resolver's job (event date),
    # not this pure function's -- it's expected that these two produce the same key.
    assert canonical_key("John Smith") == canonical_key("James Smith") == "smith_j"


def test_keys_from_title_vs():
    assert keys_from_title("Sinner vs Medvedev") == ("sinner", "medvedev")


def test_keys_from_title_v():
    assert keys_from_title("Sinner v Medvedev") == ("sinner", "medvedev")


def test_keys_from_title_v_with_period():
    assert keys_from_title("Sinner v. Medvedev") == ("sinner", "medvedev")


def test_keys_from_title_no_separator_returns_none():
    assert keys_from_title("Jannik Sinner") is None


def test_keys_from_title_empty_side_returns_none():
    assert keys_from_title(" vs Medvedev") is None


# Real Kalshi event titles are surname-only, including multi-word surnames -- a naive
# "first last" split would wrongly treat "de"/"Davidovich" as a first name (see
# surname_only_key's docstring). Titles below are real, live KXATPMATCH events.
def test_keys_from_title_multiword_surname_with_lowercase_particle():
    assert keys_from_title("de Minaur vs Svajda") == ("de_minaur", "svajda")


def test_keys_from_title_multiword_surname():
    assert keys_from_title("Davidovich Fokina vs Fucsovics") == ("davidovich_fokina", "fucsovics")


def test_keys_from_title_hyphenated_surname():
    assert keys_from_title("Auger-Aliassime vs Zheng") == ("auger_aliassime", "zheng")


def test_surname_only_key_does_not_split_first_and_last():
    assert surname_only_key("de Minaur") == "de_minaur"
    assert surname_only_key("Sinner") == "sinner"


def test_surname_key_strips_trailing_initial():
    assert surname_key("sinner_j") == "sinner"
    assert surname_key("auger_aliassime_f") == "auger_aliassime"


def test_surname_key_is_a_noop_when_no_initial():
    assert surname_key("sinner") == "sinner"
    assert surname_key("de_minaur") == "de_minaur"


def test_full_name_canonical_key_matches_title_only_surname_key():
    # The join invariant resolve_match relies on: a full-name canonical_key, stripped of
    # its initial, must equal the surname_only_key derived from a bare-surname title.
    assert surname_key(canonical_key("Alex de Minaur")) == surname_only_key("de Minaur")
    assert surname_key(canonical_key("Alejandro Davidovich Fokina")) == surname_only_key(
        "Davidovich Fokina"
    )
    assert surname_key(canonical_key("Felix Auger-Aliassime")) == surname_only_key("Auger-Aliassime")
