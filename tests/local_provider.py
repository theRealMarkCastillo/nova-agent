import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LocalProvider:
    def __init__(self):
        self.requests = []
        self.requested = threading.Event()
        self.stopping = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value, status=200):
                encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                self.send_json(
                    {
                        "data": [
                            {
                                "id": "fixture-model",
                                "object": "model",
                                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                            }
                        ]
                    }
                )

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(body)
                fixture.requested.set()
                task = next(
                    message["content"]
                    for message in reversed(body["messages"])
                    if message["role"] == "user"
                )
                if "fail-provider" in task:
                    self.send_json(
                        {
                            "error": {
                                "message": "fixture rejected request",
                                "type": "invalid_request_error",
                            }
                        },
                        400,
                    )
                    return
                content = "Fixture response: " + task
                message = {"role": "assistant", "content": content}
                if "write-artifact" in task and body["messages"][-1]["role"] != "tool":
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture-call",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": "artifact.txt", "content": "fixture artifact\n"}
                                    ),
                                },
                            }
                        ],
                    }
                usage = {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.0001,
                }
                if not body.get("stream"):
                    if "slow" in task:
                        fixture.stopping.wait(3)
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.send_json(
                            {
                                "id": "fixture",
                                "object": "chat.completion",
                                "created": 0,
                                "model": "fixture-model",
                                "choices": [
                                    {
                                        "index": 0,
                                        "finish_reason": "tool_calls"
                                        if "tool_calls" in message
                                        else "stop",
                                        "message": message,
                                    }
                                ],
                                "usage": usage,
                            }
                        )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def chunk(delta, finish=None, final_usage=None):
                    data = {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "fixture-model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    if final_usage is not None:
                        data["usage"] = final_usage
                    self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                    self.wfile.flush()

                try:
                    if "tool_calls" in message:
                        chunk({"tool_calls": [{"index": 0, **message["tool_calls"][0]}]})
                    elif "slow" in task:
                        for _ in range(200):
                            if fixture.stopping.wait(0.02):
                                break
                            chunk({"content": "."})
                    else:
                        chunk({"content": content[:8]})
                        chunk({"content": content[8:]})
                    chunk({}, "tool_calls" if "tool_calls" in message else "stop", usage)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"
