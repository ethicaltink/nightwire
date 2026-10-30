"""Motion events: discovery, lifecycle, storage and live notification.

- `topics`   flatten GetEventProperties into choosable sources. Pure, and
             deliberately free of any vendor topic string
- `tracker`  the open/close/merge/cap/reconnect rules. Pure
- `clock`    camera clock offset measurement and correction. Pure
- `store`    event sources and events in SQLite; applies tracker effects
- `listener` the ONVIF PullPoint subscription per camera
- `bus`      in-process pub/sub behind the SSE endpoint

Detection itself stays on the cameras. Nightwire records continuously and
overlays what the cameras report, so motion costs the server almost nothing.
"""
