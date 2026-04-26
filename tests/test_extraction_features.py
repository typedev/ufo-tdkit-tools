# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for FEA post-processing in extraction.converter."""

from __future__ import annotations

import textwrap

from ufo_tdkit_tools.extraction.converter import (
    _inline_aalt_lookups,
    _strip_empty_gdef,
    _strip_lookup_noise,
    _unify_feature_blocks,
)


class TestUnifyFeatureBlocks:
    def test_collapses_when_coverage_is_full(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;

            feature liga {
                script DFLT;
                language dflt;
                lookup L1;
            } liga;

            feature liga {
                script latn;
                language dflt;
                lookup L1;
            } liga;
        """)
        out = _unify_feature_blocks(fea)
        assert out.count("feature liga {") == 1
        assert "script DFLT" not in out
        assert "script latn" not in out
        assert "lookup L1" in out

    def test_emits_one_block_with_per_language_sections(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            languagesystem latn ROM;
            languagesystem latn MOL;

            feature locl {
                script latn;
                language ROM;
                lookup L13;
            } locl;

            feature locl {
                script latn;
                language MOL;
                lookup L13;
            } locl;
        """)
        out = _unify_feature_blocks(fea)
        # Only ONE feature block, with both language sections inside
        assert out.count("feature locl {") == 1
        assert "language ROM" in out
        assert "language MOL" in out
        # script latn should appear only once (elided on language switch)
        assert out.count("script latn;") == 1

    def test_merges_aalt_lookups_into_single_block(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;

            feature aalt {
                lookup L1;
            } aalt;

            feature aalt {
                lookup L2;
            } aalt;
        """)
        out = _unify_feature_blocks(fea)
        assert out.count("feature aalt {") == 1
        assert "lookup L1" in out
        assert "lookup L2" in out

    def test_handles_mixed_full_and_partial_coverage_in_one_tag(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            languagesystem latn AZE;

            feature liga {
                script DFLT;
                language dflt;
                lookup L1;
            } liga;

            feature liga {
                script latn;
                language dflt;
                lookup L1;
            } liga;

            feature liga {
                script latn;
                language AZE;
                lookup L1;
            } liga;

            feature liga {
                script latn;
                language AZE;
                lookup L2;
            } liga;
        """)
        out = _unify_feature_blocks(fea)
        # Single block, since L1 is everywhere and L2 is AZE only,
        # this needs explicit per-language sections
        assert out.count("feature liga {") == 1
        # AZE section should contain both L1 and L2
        # Order in output: DFLT/dflt L1, latn/dflt L1, latn/AZE L1+L2
        assert "lookup L2" in out

    def test_dedupes_repeated_script_language_lookup(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            languagesystem latn AZE;

            feature ss01 {
                script latn;
                language AZE;
                lookup L1;
            } ss01;

            feature ss01 {
                script latn;
                language AZE;
                lookup L1;
            } ss01;
        """)
        out = _unify_feature_blocks(fea)
        # Two identical blocks merge; AZE section accumulates L1 twice
        # (we don't currently dedupe — that would require knowing rule semantics)
        # but at minimum there's only one feature block
        assert out.count("feature ss01 {") == 1

    def test_no_languagesystems_still_unifies(self):
        fea = textwrap.dedent("""\
            feature aalt {
                lookup L1;
            } aalt;

            feature aalt {
                lookup L2;
            } aalt;
        """)
        out = _unify_feature_blocks(fea)
        assert out.count("feature aalt {") == 1
        assert "lookup L1" in out
        assert "lookup L2" in out

    def test_elides_redundant_script_statement(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            languagesystem latn AZE;
            languagesystem latn TRK;

            feature liga {
                script latn;
                language AZE;
                lookup L1;
            } liga;

            feature liga {
                script latn;
                language TRK;
                lookup L2;
            } liga;
        """)
        out = _unify_feature_blocks(fea)
        # Only one `script latn;` because we already used it
        assert out.count("script latn;") == 1
        assert "language AZE" in out
        assert "language TRK" in out


class TestInlineAaltLookups:
    def test_inlines_single_subs(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                sub a by a.alt;
                sub b by b.alt;
            } L1;

            feature aalt {
                lookup L1;
            } aalt;
        """)
        out = _inline_aalt_lookups(fea)
        # aalt now has the sub statements directly
        aalt_section = out[out.index("feature aalt"):out.index("} aalt;")]
        assert "sub a by a.alt" in aalt_section
        assert "sub b by b.alt" in aalt_section
        assert "lookup L1;" not in aalt_section
        # original named lookup is preserved
        assert "lookup L1 {" in out

    def test_inlines_alternate_subs(self):
        fea = textwrap.dedent("""\
            lookup L2 {
                sub one from [onesuperior one.alt];
                sub two from [twosuperior two.alt];
            } L2;

            feature aalt {
                lookup L2;
            } aalt;
        """)
        out = _inline_aalt_lookups(fea)
        aalt_section = out[out.index("feature aalt"):out.index("} aalt;")]
        assert "sub one from [onesuperior one.alt]" in aalt_section
        assert "sub two from [twosuperior two.alt]" in aalt_section

    def test_handles_multiple_lookup_refs(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                sub a by a.alt;
            } L1;
            lookup L2 {
                sub one from [onesuperior one.alt];
            } L2;

            feature aalt {
                lookup L1;
                lookup L2;
            } aalt;
        """)
        out = _inline_aalt_lookups(fea)
        aalt_section = out[out.index("feature aalt"):out.index("} aalt;")]
        assert "sub a by a.alt" in aalt_section
        assert "sub one from [onesuperior one.alt]" in aalt_section

    def test_leaves_other_features_alone(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                sub a by a.alt;
            } L1;

            feature ss01 {
                lookup L1;
            } ss01;
        """)
        out = _inline_aalt_lookups(fea)
        # ss01 is NOT aalt; lookup reference should remain
        assert "lookup L1;" in out
        assert "feature ss01" in out


class TestStripLookupNoise:
    def test_removes_lookupflag_zero(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                lookupflag 0;
                sub a by b;
            } L1;
        """)
        out = _strip_lookup_noise(fea)
        assert "lookupflag" not in out
        assert "sub a by b" in out

    def test_keeps_nonzero_lookupflag(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                lookupflag 8;
                sub a by b;
            } L1;
        """)
        out = _strip_lookup_noise(fea)
        assert "lookupflag 8" in out

    def test_removes_empty_statement(self):
        fea = textwrap.dedent("""\
            lookup L1 {
                ;
                sub a by b;
            } L1;
        """)
        out = _strip_lookup_noise(fea)
        assert "\n    ;\n" not in out
        assert "sub a by b" in out

    def test_removes_blank_line_before_closing_brace(self):
        fea = "feature liga {\n    lookup L1;\n\n} liga;\n"
        out = _strip_lookup_noise(fea)
        assert "    lookup L1;\n} liga;" in out


class TestStripEmptyGdef:
    def test_removes_fully_empty_gdef(self):
        fea = textwrap.dedent("""\
            languagesystem DFLT dflt;
            table GDEF {
                GlyphClassDef [], [], [], [];
            } GDEF;

            feature liga {
                lookup L1;
            } liga;
        """)
        out = _strip_empty_gdef(fea)
        assert "table GDEF" not in out
        assert "feature liga" in out

    def test_keeps_nonempty_gdef(self):
        fea = textwrap.dedent("""\
            table GDEF {
                GlyphClassDef [a b c], [], [], [];
            } GDEF;
        """)
        out = _strip_empty_gdef(fea)
        assert "table GDEF" in out
        assert "[a b c]" in out
