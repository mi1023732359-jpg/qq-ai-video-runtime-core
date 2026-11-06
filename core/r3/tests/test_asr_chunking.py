#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R2.1 P0-A: long ASR audio chunking unit tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "r2"))

from conversation_loop import split_asr_audio  # noqa: E402


class SplitAsrAudioTests(unittest.TestCase):
    def test_short_audio_one_chunk(self):
        a = np.zeros(16000 * 3, dtype=np.float32)
        chunks = split_asr_audio(a, max_s=5.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].size, a.size)

    def test_long_audio_all_samples_covered_and_capped(self):
        a = np.random.default_rng(1).normal(0, 0.05, 16000 * 12).astype(np.float32)
        chunks = split_asr_audio(a, max_s=5.0, min_s=2.5)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(sum(c.size for c in chunks), a.size)
        self.assertTrue(all(c.size <= 5.0 * 16000 for c in chunks))
        self.assertEqual(np.concatenate(chunks).size, a.size)

    def test_silent_audio_is_not_dropped(self):
        a = np.zeros(16000 * 11, dtype=np.float32)
        chunks = split_asr_audio(a, max_s=5.0)
        self.assertEqual(sum(c.size for c in chunks), a.size)
        self.assertTrue(all(c.size > 0 for c in chunks))

    def test_empty_audio(self):
        self.assertEqual(split_asr_audio(np.array([], dtype=np.float32)), [])


if __name__ == "__main__":
    unittest.main()
