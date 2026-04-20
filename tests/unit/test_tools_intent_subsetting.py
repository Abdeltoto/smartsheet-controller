"""Unit tests for intent-based tool subsetting (P1.3 + P2.x).

The subsetter shrinks the tool list shown to the LLM to save tokens, but it
must NEVER hide a write tool when the user clearly asks for a write. The
verb-token safety net (P1.3) catches inflected verbs that the substring
keyword matcher misses (e.g. "ajoute une colonne" → write_structure).

These tests pin the contract: every common French and English phrasing of
"add a row / column / sheet / share / etc." surfaces the right tool.
"""
from __future__ import annotations

import pytest

from backend.tools import (
    TOOL_DEFINITIONS,
    _tokenize_words,
    _WRITE_VERB_TOKENS,
    select_tools_for_message,
)

pytestmark = [pytest.mark.unit]


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


# ────────────────────── tokenization sanity ──────────────────────


class TestTokenizer:
    def test_splits_on_punctuation_and_whitespace(self):
        toks = _tokenize_words("Ajoute, une colonne!")
        assert {"ajoute", "une", "colonne"}.issubset(toks)

    def test_lowercases(self):
        assert "ajoute" in _tokenize_words("AJOUTE")

    def test_keeps_french_accented_chars(self):
        toks = _tokenize_words("crée déplace")
        assert "crée" in toks
        assert "déplace" in toks

    def test_drops_digits(self):
        # The regex is letters-only — digits are intentionally not tokens.
        toks = _tokenize_words("row 12")
        assert "row" in toks
        assert "12" not in toks


# ────────────────────── verb token coverage ──────────────────────


class TestWriteVerbTokens:
    """The verb set must cover the major inflections in FR + EN, otherwise
    P1.3 doesn't actually catch the bug it was built to fix."""

    @pytest.mark.parametrize("verb", [
        # English
        "add", "create", "make", "delete", "remove", "rename", "update", "modify",
        # French infinitives + common conjugations
        "ajoute", "ajouter", "rajoute", "rajouter",
        "crée", "créer", "creer",
        "supprime", "supprimer",
        "renomme", "renommer",
        "modifie", "modifier",
    ])
    def test_common_verbs_present(self, verb: str):
        assert verb in _WRITE_VERB_TOKENS, (
            f"verb '{verb}' is missing from _WRITE_VERB_TOKENS — users will say it, "
            "and write tools won't be unlocked"
        )


# ────────────────────── end-to-end intent selection ──────────────────────


READ_ONLY_TOOLS_THAT_MUST_ALWAYS_BE_PRESENT = {
    "get_sheet_summary", "read_rows", "list_sheets", "get_row",
}


class TestReadOnlyMessages:
    """Pure read intents must always expose the read core."""

    @pytest.mark.parametrize("msg", [
        "Que contient cette feuille ?",
        "What's in this sheet?",
        "Liste les colonnes de cette feuille.",
        "Show me the rows.",
        "What columns does this sheet have?",
    ])
    def test_pure_read_message_exposes_read_core(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert READ_ONLY_TOOLS_THAT_MUST_ALWAYS_BE_PRESENT.issubset(names), (
            "read core must always surface for any question"
        )
        # Note: the subsetter is intentionally permissive — when the user
        # mentions "row" or "column", the corresponding *family* (including
        # delete) is exposed. Destructive execution is gated by the confirm
        # callback at the agent level, not by the tool subset.

    def test_question_without_row_column_or_sheet_words_excludes_destructive(self):
        # No mention of row/column/sheet → no destructive tools surface.
        names = _names(select_tools_for_message("Bonjour, comment ça va ?"))
        assert "delete_rows" not in names
        assert "delete_column" not in names
        assert "delete_sheet" not in names


class TestColumnIntentVariants:
    """Every common phrasing of 'add / rename / delete a column' must surface
    the right column tool. This is the bug class that triggered P1.3."""

    @pytest.mark.parametrize("msg", [
        "ajoute une colonne",
        "Ajoute une colonne 'Owner'",
        "ajouter une colonne",
        "rajoute une colonne",
        "rajouter une colonne",
        "crée une colonne",
        "créer une colonne",
        "creer une colonne",
        "ajout de colonne",
        "Add a column",
        "create a column",
        "make a new column",
        "new column called Owner",
    ])
    def test_add_column_phrasings_unlock_add_column(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "add_column" in names, (
            f"'{msg}' should expose `add_column` to the LLM"
        )

    @pytest.mark.parametrize("msg", [
        "renomme la colonne X en Y",
        "rename column X to Y",
        "rename the Status column",
        "renommer la colonne",
    ])
    def test_rename_column_phrasings_unlock_update_column(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "update_column" in names

    @pytest.mark.parametrize("msg", [
        "supprime la colonne X",
        "supprimer la colonne",
        "delete column X",
        "remove column X",
        "drop column X",
    ])
    def test_delete_column_phrasings_unlock_delete_column(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "delete_column" in names


class TestRowIntentVariants:
    @pytest.mark.parametrize("msg", [
        "ajoute une ligne",
        "ajouter une ligne",
        "rajoute une ligne",
        "add a row",
        "create a new row",
        "insert a row",
        "ajoute une ligne avec Task='Buy milk'",
    ])
    def test_add_row_phrasings_unlock_add_rows(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "add_rows" in names

    @pytest.mark.parametrize("msg", [
        "supprime la ligne 12",
        "delete row 12",
        "remove row 12",
        "supprimer la ligne",
    ])
    def test_delete_row_phrasings_unlock_delete_rows(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "delete_rows" in names

    @pytest.mark.parametrize("msg", [
        "modifie la ligne 12",
        "update row 12",
        "change row 12",
        "edit row 12",
    ])
    def test_update_row_phrasings_unlock_update_rows(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "update_rows" in names


class TestSheetLifecycleIntents:
    @pytest.mark.parametrize("msg", [
        "crée une feuille",
        "créer une feuille",
        "create a new sheet",
        "make a sheet called Demo",
    ])
    def test_create_sheet_phrasings(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "create_sheet" in names

    @pytest.mark.parametrize("msg", [
        "renomme la feuille",
        "rename the sheet",
        "rename sheet to X",
    ])
    def test_rename_sheet_phrasings(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "rename_sheet" in names


class TestVerbTokenSafetyNetCoUnlocksBothWriteFamilies:
    """The whole point of P1.3: a single verb token unlocks BOTH `write_row`
    AND `write_structure`, because the row/column confusion is the most
    common failure mode."""

    @pytest.mark.parametrize("msg", [
        "ajoute quelque chose",  # no row/col specified
        "create something new",
        "modifie ça",
        "rajoute X",
    ])
    def test_bare_verb_unlocks_both_row_and_column_tools(self, msg: str):
        names = _names(select_tools_for_message(msg))
        # row family
        assert "add_rows" in names
        # column family — the whole reason P1.3 exists
        assert "add_column" in names


class TestEmptyAndEdgeCases:
    def test_empty_message_returns_all_tools(self):
        result = select_tools_for_message("")
        assert len(result) == len(TOOL_DEFINITIONS), (
            "with no message we can't infer intent — must return everything"
        )

    def test_unrelated_chitchat_keeps_subset_small_but_above_floor(self):
        # Small talk: only the read core should fire. The safety floor (>= 8
        # tools) ensures we don't hand the LLM an empty list.
        result = select_tools_for_message("Hello, how are you today?")
        assert len(result) >= 8
        # Should not contain destructive write tools
        names = _names(result)
        assert "delete_sheet" not in names
        assert "delete_rows" not in names


class TestCrossSheetIntents:
    """Pulling data from another sheet REQUIRES `create_cross_sheet_ref`. The
    verb-keyword filter previously masked this tool because none of the
    natural phrasings ('ramène', 'récupère', 'lookup', 'from another sheet')
    were registered. These tests pin the contract so the regression cannot
    come back silently: any common cross-sheet phrasing must surface BOTH
    `create_cross_sheet_ref` and `list_cross_sheet_refs`, plus the
    discoverability tools (`list_sheets`, `get_sheet_summary`) needed to
    follow the workflow.
    """

    @pytest.mark.parametrize("msg", [
        # FR
        "ramène la valeur Status de la sheet Customers",
        "récupère le prix depuis l'autre feuille",
        "Importe la colonne Status depuis ma sheet Source",
        "fais un lookup sur l'autre sheet",
        "vlookup le SKU dans la feuille Catalogue",
        "j'ai besoin de tirer les valeurs d'une autre sheet",
        "rejoindre la feuille Source pour récupérer le prix",
        "référence vers une autre feuille",
        "crée une cross-sheet reference",
        # EN
        "pull the price from another sheet",
        "I want to lookup values from another sheet",
        "VLOOKUP the SKU from the Catalogue sheet",
        "create a cross-sheet reference to Customers",
        "bring data from the other sheet into this one",
        "join the Source sheet to fetch the status",
        "I need an INDEX MATCH across sheets",
    ])
    def test_cross_sheet_phrasings_unlock_create_cross_sheet_ref(self, msg: str):
        names = _names(select_tools_for_message(msg))
        assert "create_cross_sheet_ref" in names, (
            f"'{msg}' must expose `create_cross_sheet_ref` to the LLM — "
            f"otherwise the agent literally cannot build the reference"
        )

    @pytest.mark.parametrize("msg", [
        "ramène la valeur Status de la sheet Customers",
        "lookup the price from the Catalogue sheet",
        "récupère le prix depuis l'autre feuille",
    ])
    def test_cross_sheet_phrasings_also_expose_list_cross_sheet_refs(self, msg: str):
        # `list_cross_sheet_refs` lets the agent discover already-existing
        # named refs and avoid duplicates.
        names = _names(select_tools_for_message(msg))
        assert "list_cross_sheet_refs" in names

    @pytest.mark.parametrize("msg", [
        "ramène la valeur Status de la sheet Customers",
        "lookup the price from the Catalogue sheet",
        "I want to pull the status from another sheet",
    ])
    def test_cross_sheet_workflow_also_exposes_discovery_and_writers(self, msg: str):
        # The full cross-sheet workflow is: list_sheets / search → get summary
        # → create_cross_sheet_ref → write the formula via add_rows /
        # update_rows / add_column. All these tools must be in the subset for
        # the agent to actually finish the job in a single turn.
        names = _names(select_tools_for_message(msg))
        assert "list_sheets" in names, "needed to find the source sheet ID"
        assert "get_sheet_summary" in names, "needed to obtain source column IDs"
        # add_column is the column-formula path (most common for cross-sheet
        # lookups that should apply to every row); add_rows / update_rows are
        # the per-cell paths.
        assert "add_column" in names
        assert "add_rows" in names
