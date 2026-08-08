from gateway.platforms.voice import VoiceAdapter


def test_voice_adapter_suppresses_runtime_and_compaction_output():
    blocked = [
        "We need to continue from the summary. The user is testing voice.",
        "Context compaction completed for this session.",
        "Tool memory returned error: refusing to write MEMORY.md.",
        "Error: Codex app-server exited: code=1 signal=null",
        "System message: Hermes Gateway Starting",
        "File \"/tmp/script.py\", line 3, in <module>",
        "[This response was interrupted by a user correction.]",
    ]

    for text in blocked:
        assert VoiceAdapter._should_suppress_voice_output(text)


def test_voice_adapter_allows_normal_spoken_reply():
    assert not VoiceAdapter._should_suppress_voice_output(
        "I can do that. Give me a second to check it."
    )
