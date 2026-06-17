def test_append_past_milestone_does_not_raise():
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    b = SessionTranscriptBuffer()
    for i in range(1001):   # crosses the 1000-event milestone log line
        b.append("s", {"i": i})
    assert len(b.events("s")) == 1001
