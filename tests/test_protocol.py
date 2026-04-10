import base64
import json

from protocol import encode, decode_all, Frame


def test_encode_produces_expected_shape():
    out = encode("turn", {"state": "done"})
    assert out.startswith("<<<CLIVE:turn:")
    assert out.endswith(">>>")
    # Payload must be base64-encoded JSON
    b64 = out[len("<<<CLIVE:turn:"):-len(">>>")]
    assert json.loads(base64.b64decode(b64).decode()) == {"state": "done"}


def test_decode_single_frame():
    screen = "random output\n" + encode("turn", {"state": "thinking"}) + "\nmore output\n"
    frames = decode_all(screen)
    assert len(frames) == 1
    assert frames[0] == Frame(kind="turn", payload={"state": "thinking"})


def test_decode_multiple_frames_preserves_order():
    screen = "\n".join([
        encode("turn", {"state": "thinking"}),
        "some shell output",
        encode("context", {"result": "ok"}),
        encode("turn", {"state": "done"}),
    ])
    frames = decode_all(screen)
    assert [f.kind for f in frames] == ["turn", "context", "turn"]
    assert frames[-1].payload == {"state": "done"}


def test_decode_ignores_stray_text_that_looks_like_sentinel():
    # A tool printing the literal string <<<CLIVE:turn:done>>> (no valid base64)
    # must not be parsed as a frame.
    screen = "<<<CLIVE:turn:done>>>\n"  # 'done' is not valid base64
    frames = decode_all(screen)
    assert frames == []


def test_decode_tolerates_partial_frame_at_start():
    # Simulates tmux scrollback truncation mid-frame.
    partial = "CLIVE:turn:" + "eyJzdGF0ZSI6ImRvbmUifQ==" + ">>>"
    good = encode("turn", {"state": "done"})
    frames = decode_all(partial + "\n" + good)
    assert len(frames) == 1
    assert frames[0].payload == {"state": "done"}


def test_decode_rejects_non_dict_payload():
    bad = "<<<CLIVE:turn:" + base64.b64encode(b'"just a string"').decode() + ">>>"
    frames = decode_all(bad)
    assert frames == []


def test_kinds_is_the_source_of_truth():
    from protocol import KINDS
    assert {"turn", "context", "question", "file", "progress",
            "llm_request", "llm_response", "llm_error", "alive"} <= set(KINDS)
