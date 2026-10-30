"""Live video: go2rtc supervision and the browser-facing signalling proxy.

- `config`  stream names and the go2rtc config document. Pure
- `client`  go2rtc's HTTP API, lazily importing httpx
- `process` the go2rtc child process and its stream sync

go2rtc carries live video only. The recorder pulls RTSP straight from each
camera, so nothing here can interrupt recording.
"""
