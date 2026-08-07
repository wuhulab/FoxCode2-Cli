import json
import warnings
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    UserPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    ModelMessagesTypeAdapter,
    narrow_message_parts,
)


def rt(name, msgs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        d = ModelMessagesTypeAdapter.dump_python(msgs, mode="json")
        raw = json.dumps(d, ensure_ascii=False)
        try:
            back = ModelMessagesTypeAdapter.validate_python(json.loads(raw))
            print(f"{name}: OK", [type(m).__name__ for m in back])
            for m in back:
                for p in m.parts:
                    print(
                        "   ",
                        type(p).__name__,
                        getattr(p, "tool_name", ""),
                        getattr(p, "tool_call_id", ""),
                    )
        except Exception as e:
            print(f"{name}: FAIL:", str(e)[:120])


req = ModelRequest(
    parts=[
        UserPromptPart(content="你好，请读取文件"),
        ToolReturnPart(
            tool_name="read_file", content="文件内容", tool_call_id="call_1"
        ),
    ]
)
resp = ModelResponse(
    parts=[
        ToolCallPart(
            tool_name="read_file", args={"path": "a.py"}, tool_call_id="call_1"
        ),
        TextPart(content="已读取"),
    ]
)

rt("dict-args", [req, resp])
rt("narrow dict-args", narrow_message_parts([req, resp]))

resp_str = ModelResponse(
    parts=[
        ToolCallPart(
            tool_name="read_file", args='{"path":"a.py"}', tool_call_id="call_1"
        ),
    ]
)
rt("str-args", [req, resp_str])
rt("narrow str-args", narrow_message_parts([req, resp_str]))
