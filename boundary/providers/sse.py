"""A server-sent events decoder: bytes in, the `data:` payload of each complete event out.

Pure and incremental, so it can be fed the chunks a streaming response arrives in and asked
after each one which events completed. That is what lets the gateway stamp the moment the
first content delta arrived rather than the moment the stream ended.

Only `data:` lines are kept. `event:`, `id:` and `retry:` fields and comment lines are read
and dropped: no OpenAI-compatible host puts anything in them that the chat completion
object does not also carry. Multi-line data is joined with newlines, as the specification
says, and both `\\n` and `\\r\\n` line endings are accepted because hosts differ.
"""

from __future__ import annotations


class SSEDecoder:
    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[str] = []

    def feed(self, chunk: bytes) -> list[str]:
        """Read one chunk of the body and return the data payloads of the events it
        completed, in order. An event is complete at its first blank line."""
        self._buffer += chunk
        out: list[str] = []
        while True:
            nl = self._buffer.find(b"\n")
            if nl < 0:
                break
            line = self._buffer[:nl]
            self._buffer = self._buffer[nl + 1 :]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                if self._data:
                    out.append("\n".join(self._data))
                    self._data = []
                continue
            if line.startswith(b":"):
                continue
            field, sep, value = line.partition(b":")
            if not sep:
                field, value = line, b""
            if value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                self._data.append(value.decode("utf-8", "replace"))
        return out

    def finish(self) -> list[str]:
        """The stream has ended. An event without a trailing blank line is still an event."""
        out: list[str] = []
        if self._buffer:
            out.extend(self.feed(b"\n"))
        if self._data:
            out.append("\n".join(self._data))
            self._data = []
        return out
