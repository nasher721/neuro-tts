"""Unit tests for the speech-normalization layer (pure, Qt-free)."""

import unittest

try:
    from . import text_normalize
except ImportError:  # pragma: no cover - supports PYTHONPATH=addon discovery
    from addon import text_normalize

normalize = text_normalize.normalize_for_speech
split = text_normalize.split_for_synthesis


class NormalizeTests(unittest.TestCase):
    def test_empty_input_is_empty_output(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")

    def test_cloze_markup_keeps_the_answer_and_drops_the_hint(self):
        self.assertEqual(normalize("{{c1::Nimodipine::drug}} helps"), "Nimodipine helps")
        self.assertEqual(normalize("{{c2::60 mg}}"), "60 milligrams")

    def test_other_anki_field_markup_is_removed(self):
        self.assertEqual(normalize("before {{type:Front}} after"), "before after")

    def test_dose_shorthand_becomes_spoken_units(self):
        self.assertEqual(normalize("10mg"), "10 milligrams")
        self.assertEqual(normalize("0.5 g/kg"), "0.5 grams per kilogram")
        self.assertEqual(normalize("22 mmHg"), "22 millimeters of mercury")

    def test_dosing_interval_shorthand(self):
        self.assertIn("every 6 hours", normalize("q6h"))
        self.assertIn("every 4 to 6 hours", normalize("q4-6h"))
        self.assertIn("every 1 hour", normalize("q1h"))

    def test_numeric_ranges_read_as_ranges(self):
        self.assertEqual(normalize("goal 65-70"), "goal 65 to 70")

    def test_comparisons_are_spoken_only_next_to_numbers(self):
        self.assertEqual(normalize("SBP <140"), "systolic blood pressure less than 140")
        self.assertEqual(normalize("ICP >=20"), "intracranial pressure greater than or equal to 20")

    def test_arrows_and_trend_symbols(self):
        self.assertEqual(normalize("A -> B"), "A leads to B")
        self.assertEqual(normalize("↑ICP"), "increased intracranial pressure")
        self.assertEqual(normalize("↓CPP"), "decreased cerebral perfusion pressure")

    def test_neuro_abbreviations_are_expanded(self):
        spoken = normalize("Pt w/ SAH s/p EVD placement")
        self.assertEqual(spoken, "patient with subarachnoid hemorrhage status post external ventricular drain placement")

    def test_abbreviation_expansion_can_be_disabled(self):
        self.assertEqual(normalize("SAH", expand_abbreviations=False), "SAH")
        # Symbol and unit handling stays on regardless.
        self.assertEqual(normalize("10mg", expand_abbreviations=False), "10 milligrams")

    def test_case_sensitive_abbreviations_do_not_eat_ordinary_words(self):
        # "MS" is multiple sclerosis; the unit "ms" must survive intact.
        self.assertIn("multiple sclerosis", normalize("MS relapse"))
        self.assertEqual(normalize("250 ms"), "250 milliseconds")

    def test_abbreviations_only_match_whole_words(self):
        self.assertEqual(normalize("PILOT"), "PILOT")
        self.assertEqual(normalize("computer"), "computer")

    def test_user_replacements_win_over_builtins(self):
        self.assertEqual(
            normalize("SAH today", extra_replacements={"SAH": "sub arachnoid bleed"}),
            "sub arachnoid bleed today",
        )

    def test_percentages_and_fractions(self):
        self.assertEqual(normalize("50%"), "50 percent")
        self.assertEqual(normalize("2/3 of cases"), "2 out of 3 of cases")

    def test_duration_shorthand(self):
        self.assertIn("for 21 days", normalize("nimodipine x 21 days"))

    def test_whitespace_and_punctuation_are_tidied(self):
        self.assertEqual(normalize("a  ,  b .  "), "a, b.")

    def test_output_is_stable_under_repeat_application(self):
        once = normalize("Pt w/ SAH, ICP 22 mmHg -> mannitol 0.5 g/kg q6h.")
        self.assertEqual(normalize(once), once)


class SplitTests(unittest.TestCase):
    def test_short_text_is_a_single_chunk(self):
        self.assertEqual(split("Hello there.", 100), ["Hello there."])

    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(split("", 100), [])
        self.assertEqual(split("   ", 100), [])

    def test_splits_on_sentence_boundaries(self):
        self.assertEqual(split("One. Two. Three. Four.", 12), ["One. Two.", "Three. Four."])

    def test_every_chunk_respects_the_limit(self):
        text = " ".join(f"Sentence number {n} about neurocritical care." for n in range(60))
        for chunk in split(text, 200):
            self.assertLessEqual(len(chunk), 200)

    def test_no_content_is_lost_when_splitting(self):
        text = "Alpha beta. Gamma delta. Epsilon zeta."
        self.assertEqual(" ".join(split(text, 15)), text)

    def test_an_over_long_sentence_falls_back_to_clauses(self):
        sentence = "alpha, " * 40
        chunks = split(sentence.strip(), 100)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)

    def test_a_single_unbreakable_token_is_hard_split(self):
        chunks = split("x" * 250, 100)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), "x" * 250)

    def test_non_positive_limit_disables_splitting(self):
        self.assertEqual(split("One. Two.", 0), ["One. Two."])


if __name__ == "__main__":
    unittest.main()
