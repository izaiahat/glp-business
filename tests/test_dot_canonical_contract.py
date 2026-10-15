"""Offline wire-format and SHA-256 regressions for dispatcher identities."""
from copy import deepcopy
from itertools import permutations

import pytest

from scripts import dot_dispatch as d


def test_canonical_preserves_json_types_without_extra_whitespace():
    value = {"z": {}, "a": [None, True, False, 0, -7, 1.0, 1.25, "1", []]}

    assert d.canonical(value) == '{"a":[null,true,false,0,-7,1.0,1.25,"1",[]],"z":{}}'


@pytest.mark.parametrize("keys", list(permutations(("z", "items", "a"))))
@pytest.mark.parametrize("nested_keys", [("z", "a"), ("a", "z")])
def test_mapping_insertion_order_cannot_change_canonical_identity(keys, nested_keys):
    nested = {key: {"a": 1, "z": 2}[key] for key in nested_keys}
    values = {"z": 0, "items": ["second", "first"], "a": nested}
    value = {key: values[key] for key in keys}

    text = d.canonical(value)

    assert text == '{"a":{"a":1,"z":2},"items":["second","first"],"z":0}'
    assert d.digest(text) == '458cb68482a899df54b74e4dc50d45a9273324164cbd922a91078963e1191a7b'


def test_canonical_escapes_unicode_keys_values_and_control_characters():
    value = {"中": '\n\t"\\', "é": "e\u0301", "a": "café/中/🙂"}

    text = d.canonical(value)

    assert text == r'{"a":"caf\u00e9/\u4e2d/\ud83d\ude42","\u00e9":"e\u0301","\u4e2d":"\n\t\"\\"}'
    assert text.isascii()
    assert d.digest(text) == '92f3c7440eee88c33a6a3b65af5e00437ac570638dd4470e5a5fe58b28b07a31'


def test_canonical_distinguishes_unicode_from_literal_escape_text():
    assert d.canonical("é") == r'"\u00e9"'
    assert d.canonical(r"\u00e9") == r'"\\u00e9"'
    assert d.digest(d.canonical("é")) != d.digest(d.canonical(r"\u00e9"))


def test_canonical_does_not_normalize_unicode():
    composed = d.canonical({"name": "é"})
    decomposed = d.canonical({"name": "e\u0301"})

    assert composed == r'{"name":"\u00e9"}'
    assert decomposed == r'{"name":"e\u0301"}'
    assert d.digest(composed) != d.digest(decomposed)


def test_array_order_remains_part_of_canonical_identity():
    forward = d.canonical({"items": [{"z": 2, "a": 1}, "last"]})
    reverse = d.canonical({"items": ["last", {"a": 1, "z": 2}]})

    assert forward == '{"items":[{"a":1,"z":2},"last"]}'
    assert reverse == '{"items":["last",{"a":1,"z":2}]}'
    assert d.digest(forward) != d.digest(reverse)


def test_canonical_is_repeatable_without_mutating_nested_input():
    value = {"z": [{"β": [3, 2, 1], "a": "🙂"}], "a": {"y": 2, "x": 1}}
    before = deepcopy(value)
    keys_before = (list(value), list(value["a"]), list(value["z"][0]))

    outputs = [d.canonical(value) for _ in range(3)]

    assert outputs == [r'{"a":{"x":1,"y":2},"z":[{"a":"\ud83d\ude42","\u03b2":[3,2,1]}]}'] * 3
    assert value == before
    assert (list(value), list(value["a"]), list(value["z"][0])) == keys_before


@pytest.mark.parametrize(("text", "expected"), [
    ("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    ("café🙂", "acef77f84ef633e211246c7fcabb48eb8eb13b49271ec9a381f598ab4620fea3"),
])
def test_digest_has_fixed_sha256_utf8_vectors(text, expected):
    assert d.digest(text) == expected


def test_digest_hashes_exact_text_without_trimming_or_canonicalizing():
    texts = ['{"a":1}', '{ "a": 1 }', '{"a":1}\n', '{"A":1}', '{"a":1}\x00']

    assert len({d.digest(text) for text in texts}) == len(texts)
