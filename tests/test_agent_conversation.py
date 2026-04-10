from protocol import encode
from remote import parse_question


def test_parse_question():
    screen = encode("question", {"text": "What format should the output be in?"})
    assert parse_question(screen) == "What format should the output be in?"


def test_parse_question_none_when_no_question():
    screen = encode("turn", {"state": "thinking"}) + "\n" + encode("progress", {"text": "step 1 of 3"})
    assert parse_question(screen) is None


def test_parse_question_last_wins():
    screen = "\n".join([
        encode("question", {"text": "first question"}),
        encode("turn", {"state": "waiting"}),
        encode("question", {"text": "second question"}),
    ])
    assert parse_question(screen) == "second question"


def test_parse_question_empty_question():
    screen = encode("question", {"text": ""})
    assert parse_question(screen) is None
