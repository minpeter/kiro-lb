import pytest

from kiro.parsers import split_bracket_call_text


class TestSplitBracketCallText:
    """A bracket-style call becomes a tool_use block, so its text must not also
    reach the client as prose. The marker can straddle a chunk boundary, so the
    filter carries state between chunks."""

    CALL = '[Called Write with args: {"file_path": "/tmp/a.py", "content": "print(1)"}]'

    def _stream(self, chunks):
        held = ""
        emitted = []
        for chunk in chunks:
            emit, held = split_bracket_call_text(held + chunk)
            emitted.append(emit)
        return "".join(emitted), held

    def test_ordinary_text_passes_through_untouched(self):
        emitted, held = self._stream(["Ola mundo", ", tudo bem?"])
        assert emitted == "Ola mundo, tudo bem?"
        assert held == ""

    def test_a_whole_call_in_one_chunk_is_withheld(self):
        emitted, held = self._stream(["Vou criar.\n", self.CALL, "\nPronto."])
        assert emitted == "Vou criar.\n\nPronto."
        assert held == ""

    @pytest.mark.parametrize("size", [1, 3, 7, 9, 40])
    def test_a_call_split_across_chunks_is_withheld(self, size):
        pieces = [self.CALL[i : i + size] for i in range(0, len(self.CALL), size)]
        emitted, held = self._stream(["antes "] + pieces + [" depois"])
        assert "[Called" not in emitted
        assert emitted == "antes  depois"
        assert held == ""

    def test_the_closing_bracket_does_not_leak_when_it_arrives_late(self):
        emitted, held = self._stream(["texto ", "[Called Write with args: ", '{"a": 1}', "]", " fim"])
        assert emitted == "texto  fim"
        assert held == ""

    def test_brackets_that_are_not_a_call_survive(self):
        emitted, held = self._stream(["uma lista [1, 2, 3] e outra [a, b]"])
        assert emitted == "uma lista [1, 2, 3] e outra [a, b]"
        assert held == ""

    def test_a_partial_marker_is_held_rather_than_forwarded(self):
        emit, held = split_bracket_call_text("termina com [Cal")
        assert emit == "termina com "
        assert held == "[Cal"

    def test_a_lone_bracket_is_held_only_until_it_is_resolved(self):
        emit, held = split_bracket_call_text("abre [")
        assert emit == "abre "
        assert held == "["

        emit, held = split_bracket_call_text(held + "1, 2]")
        assert emit == "[1, 2]"
        assert held == ""

    def test_prose_that_merely_mentions_the_marker_is_released(self):
        # No JSON follows, so it is not a call and must not be swallowed.
        text = "[Called sem json algum, apenas texto seguindo " + "x" * 80
        emitted, held = self._stream([text])
        assert emitted == text
        assert held == ""
