from ai.messages import (
    UserMessage,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
)


def test_user_message():
    msg = UserMessage(content="hello")

    assert msg.role == "user"
    assert msg.content == "hello"


def test_assistant_message():
    msg = AssistantMessage(content="hi")

    assert msg.role == "assistant"
    assert msg.content == "hi"
    assert msg.tool_calls == []


def test_assistant_message_with_tool_call():
    call = ToolCall(
        id="call_1",
        name="read",
        arguments={"path": "main.py"},
    )

    msg = AssistantMessage(
        content="",
        tool_calls=[call],
    )

    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "read"
    assert msg.tool_calls[0].arguments["path"] == "main.py"


def test_tool_result_message():
    msg = ToolResultMessage(
        tool_call_id="call_1",
        name="read",
        content="print('hello')",
    )

    assert msg.role == "tool"
    assert msg.tool_call_id == "call_1"
    assert msg.is_error is False