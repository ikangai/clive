# tests/test_agent_conversation.py
from remote import parse_question

def test_parse_question():
    screen = "TURN: waiting\nQUESTION: What format should the output be in?"
    q = parse_question(screen)
    assert q == "What format should the output be in?"

def test_parse_question_none_when_no_question():
    screen = "TURN: thinking\nPROGRESS: step 1 of 3"
    q = parse_question(screen)
    assert q is None

def test_parse_question_multiline():
    """Should get the last QUESTION: line."""
    screen = "QUESTION: first question\nTURN: waiting\nQUESTION: second question"
    q = parse_question(screen)
    assert q == "second question"

def test_parse_question_with_extra_whitespace():
    screen = "TURN: waiting\nQUESTION:   spaces around   "
    q = parse_question(screen)
    assert q == "spaces around"

def test_parse_question_empty_question():
    screen = "TURN: waiting\nQUESTION:"
    q = parse_question(screen)
    assert q is None  # empty question should be treated as no question
