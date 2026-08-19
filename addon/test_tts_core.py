import unittest

try:
    from .tts_core import digest_for, filename, generation_digest, managed_extra, needs_update, source_text, state_for
except ImportError:  # Supports running this file from the add-on directory.
    from tts_core import digest_for, filename, generation_digest, managed_extra, needs_update, source_text, state_for


class TTSCoreTests(unittest.TestCase):
    def test_normalizes_html_images_entities_and_old_audio(self):
        extra = '<div>ICP &gt; 22 mmHg<br><img src="x.png"> Treat [sound:old.mp3]</div>'
        self.assertEqual(source_text(extra), "ICP > 22 mmHg Treat")

    def test_managed_extra_replaces_only_addon_owned_audio(self):
        extra = "Explanation [sound:user.mp3] <!-- neuroicu-tts:v1:" + "a" * 64 + " --> [sound:old.mp3]"
        digest = digest_for("Explanation")
        result = managed_extra(extra, filename(123, digest), digest)
        self.assertEqual(result.count("[sound:"), 2)
        self.assertIn(filename(123, digest), result)
        self.assertIn("user.mp3", result)
        self.assertNotIn("old.mp3", result)

    def test_state_ignores_unmanaged_audio(self):
        state = state_for("Explanation [sound:user.mp3]")
        self.assertIsNone(state.sound_filename)

    def test_state_hash_changes_when_explanation_changes(self):
        first = state_for("<p>Initial explanation</p>")
        second = state_for("<p>Edited explanation</p>")
        self.assertNotEqual(first.digest, second.digest)

    def test_marker_without_audio_is_still_managed_metadata(self):
        digest = digest_for("Explanation")
        state = state_for(f"Explanation <!-- neuroicu-tts:v1:{digest} -->")
        self.assertEqual(state.marker_digest, digest)
        self.assertIsNone(state.sound_filename)
        self.assertTrue(needs_update(f"Explanation <!-- neuroicu-tts:v1:{digest} -->"))

    def test_managed_extra_removes_orphaned_and_duplicate_metadata(self):
        old = "a" * 64
        extra = f"Explanation <!-- neuroicu-tts:v1:{old} --> <!-- neuroicu-tts:v1:{old} --> [sound:old.mp3]"
        digest = digest_for("Explanation")
        result = managed_extra(extra, filename(123, digest), digest)
        self.assertEqual(result.count("neuroicu-tts:v1:"), 1)
        self.assertEqual(result.count("[sound:"), 1)
        self.assertIn(filename(123, digest), result)

    def test_needs_update_accepts_any_note_id_for_matching_managed_audio(self):
        digest = digest_for("Explanation")
        extra = managed_extra("Explanation", filename(123, digest), digest)
        self.assertFalse(needs_update(extra))

    def test_v2_marker_tracks_source_and_synthesis_profile(self):
        source_digest = digest_for("Explanation")
        profile_digest = digest_for("voice-a")
        artifact_digest = generation_digest(source_digest, profile_digest)
        extra = managed_extra("Explanation", filename(42, artifact_digest), source_digest, profile_digest)

        state = state_for(extra)

        self.assertEqual(state.marker_digest, source_digest)
        self.assertEqual(state.profile_digest, profile_digest)
        self.assertFalse(needs_update(extra, profile_digest))
        self.assertTrue(needs_update(extra, digest_for("voice-b")))

    def test_v1_marker_requires_migration_when_profile_is_known(self):
        source_digest = digest_for("Explanation")
        extra = managed_extra("Explanation", filename(42, source_digest), source_digest)
        self.assertTrue(needs_update(extra, digest_for("voice-a")))


if __name__ == "__main__":
    unittest.main()
