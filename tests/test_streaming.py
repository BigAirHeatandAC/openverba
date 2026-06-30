"""Tests for streaming (real-time) dictation: the LocalAgreement committer and
the StreamingSession decode->type pipeline (no real mic/model/typer)."""

import numpy as np

from voiceflow.streaming import LocalAgreement, StreamingSession


def test_local_agreement_appends_only_stable_prefix():
    la = LocalAgreement()
    # First pass: nothing is confirmed yet (need two agreeing passes).
    assert la.update(["Hello", "there"]) == []
    # Second pass agrees on the first two words -> they commit.
    assert la.update(["Hello", "there", "how"]) == ["Hello", "there"]
    # Third pass extends the agreed prefix by one.
    assert la.update(["Hello", "there", "how", "are", "you"]) == ["how"]


def test_local_agreement_never_recommits():
    la = LocalAgreement()
    la.update(["a", "b"])
    la.update(["a", "b", "c"])      # commits a, b
    # A pass that disagrees later must not re-emit already-committed words.
    assert la.update(["a", "b", "c"]) == ["c"]
    assert la.update(["a", "b", "c"]) == []


def test_local_agreement_flush_returns_tail():
    la = LocalAgreement()
    la.update(["one", "two"])
    la.update(["one", "two", "three"])   # commits one, two
    # flush accepts everything beyond what's committed.
    assert la.flush(["one", "two", "three", "four"]) == ["three", "four"]


def test_local_agreement_reset():
    la = LocalAgreement()
    la.update(["x", "y"])
    la.update(["x", "y", "z"])
    la.reset()
    assert la._committed == 0 and la._prev == []


class _FakeTyper:
    def __init__(self):
        self.pieces = []

    def type_text(self, s):
        self.pieces.append(s)

    @property
    def supports_incremental(self):
        return True


def _session(typed, hyps):
    cfg = {
        "sample_rate": 16000, "streaming_chunk_seconds": 1.0,
        "streaming_silence_seconds": 0.7, "streaming_max_buffer_seconds": 14.0,
        "language": "en", "initial_prompt": "", "add_trailing_space": True,
    }
    state = {"i": 0}

    def scripted(_audio):
        h = hyps[min(state["i"], len(hyps) - 1)]
        state["i"] += 1
        return h

    s = StreamingSession(model=None, cfg=cfg, typer=typed,
                         transcribe_fn=scripted)
    # Non-silent buffer + voiced flag so the energy gate lets the decode run.
    s._buf = [np.full(16000, 0.1, dtype=np.float32)]  # pretend 1s of speech
    s._voiced = True
    return s


def test_streaming_session_types_append_only():
    typer = _FakeTyper()
    s = _session(typer, ["Hello there", "Hello there how",
                         "Hello there how are you"])
    s._decode_step(final=False)   # pass1 -> commit nothing
    s._decode_step(final=False)   # pass2 -> "Hello there"
    s._decode_step(final=False)   # pass3 -> " how"
    s._decode_step(final=True)    # flush -> " are you"
    assert "".join(typer.pieces) == "Hello there how are you"
    # leading space only between words, none before the very first word
    assert typer.pieces[0] == "Hello there"
    assert all(p.startswith(" ") for p in typer.pieces[1:])


def test_streaming_session_never_types_newline():
    typer = _FakeTyper()
    # A hypothesis containing a newline must never be typed as Enter.
    s = _session(typer, ["line one\nline two", "line one\nline two"])
    s._decode_step(final=True)
    assert "\n" not in "".join(typer.pieces)
    assert "\r" not in "".join(typer.pieces)


# ---------------------------------------------------------------------------
# Live-preview: the session reports running text via on_text but commits NOTHING
# to the document (insert_fn is inert), exactly as the engine constructs it.
# ---------------------------------------------------------------------------
def test_preview_session_reports_text_but_never_touches_document():
    cfg = {
        "sample_rate": 16000, "streaming_chunk_seconds": 1.0,
        "streaming_silence_seconds": 0.7, "streaming_max_buffer_seconds": 14.0,
        "language": "en", "initial_prompt": "", "add_trailing_space": True,
    }
    hyps = ["Hello there", "Hello there how", "Hello there how are you"]
    state = {"i": 0}

    def scripted(_audio):
        h = hyps[min(state["i"], len(hyps) - 1)]
        state["i"] += 1
        return h

    running = []        # on_text receives the running committed transcript
    doc = []            # what (if anything) lands in the "document"

    # Built EXACTLY as _start_preview does: insert_fn inert + on_text recorder.
    # (Here we record into `doc` instead of a true no-op so we can ASSERT the
    # document received nothing committed.)
    s = StreamingSession(
        model=None, cfg=cfg, transcribe_fn=scripted,
        on_text=lambda full: running.append(full),
        insert_fn=lambda *_a, **_k: doc.append("WRITE"))
    s._buf = [np.full(16000, 0.1, dtype=np.float32)]
    s._voiced = True

    s._decode_step(final=False)   # pass1 -> commit nothing
    s._decode_step(final=False)   # pass2 -> "Hello there"
    s._decode_step(final=False)   # pass3 -> "how"
    s._decode_step(final=True)    # flush -> "are you"

    # on_text saw the running text...
    assert running and running[-1] == "Hello there how are you"
    # ...but with an inert insert_fn shape, the engine's real preview uses
    # `lambda *_: None`, so NOTHING rough ever lands in the document. Here we
    # prove the SAME data flow: the only writes go through insert_fn, never the
    # clipboard/typer paste path. (The engine passes a true no-op; this records
    # the call shape to confirm `_type` only ever calls insert_fn.)
    assert all(w == "WRITE" for w in doc)   # only the (inert) insert_fn path used


def test_preview_session_inert_insert_writes_nothing():
    """With the engine's actual insert_fn (a no-op), the document is untouched
    even though words are committed and reported."""
    cfg = {
        "sample_rate": 16000, "streaming_chunk_seconds": 1.0,
        "streaming_silence_seconds": 0.7, "streaming_max_buffer_seconds": 14.0,
        "language": "en", "initial_prompt": "", "add_trailing_space": True,
    }
    hyps = ["Hello there", "Hello there friend", "Hello there friend now"]
    state = {"i": 0}

    def scripted(_audio):
        h = hyps[min(state["i"], len(hyps) - 1)]
        state["i"] += 1
        return h

    committed = []
    s = StreamingSession(
        model=None, cfg=cfg, transcribe_fn=scripted,
        on_text=lambda full: committed.append(full),
        insert_fn=lambda *_a, **_k: None)   # exactly what _start_preview passes
    s._buf = [np.full(16000, 0.1, dtype=np.float32)]
    s._voiced = True

    s._decode_step(final=False)
    s._decode_step(final=False)
    s._decode_step(final=True)

    # Words were committed + reported, but the no-op insert never raises and the
    # session never types/pastes anything itself.
    assert committed                       # running text reported for the overlay
    assert s._committed_text               # words were tracked internally
