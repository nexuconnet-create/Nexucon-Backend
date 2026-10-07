"""
Offline sync queue (Inspector PWA — Part 3, dual-path ingestion).

The PWA captures in the field, where there is frequently no network. Writes
made offline cannot be held in memory: the app may be killed, the tablet may
be swapped, the inspector may hand the device to a colleague. So every offline
write is journalled client-side and replayed here when a connection returns.

The queue is deliberately *server-side*. A client-only queue cannot answer the
two questions that matter after a reconnect: "what did I already send?" and
"what is stuck?". Both are answered by the same row — this one.
"""
