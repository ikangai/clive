from protocol import encode
from remote import parse_question

NONCE = "testnonce"


def test_parse_question():
    screen = encode("question", {"text": "What format should the output be in?"}, nonce=NONCE)
    assert parse_question(screen, nonce=NONCE) == "What format should the output be in?"


def test_parse_question_none_when_no_question():
    screen = encode("turn", {"state": "thinking"}, nonce=NONCE) + "\n" + encode("progress", {"text": "step 1 of 3"}, nonce=NONCE)
    assert parse_question(screen, nonce=NONCE) is None


def test_parse_question_last_wins():
    screen = "\n".join([
        encode("question", {"text": "first question"}, nonce=NONCE),
        encode("turn", {"state": "waiting"}, nonce=NONCE),
        encode("question", {"text": "second question"}, nonce=NONCE),
    ])
    assert parse_question(screen, nonce=NONCE) == "second question"


def test_parse_question_empty_question():
    screen = encode("question", {"text": ""}, nonce=NONCE)
    assert parse_question(screen, nonce=NONCE) is None
