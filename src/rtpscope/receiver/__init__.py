# receiver --> python call client that only receives, with our own jitter buffer and a/v sync
#
#   client.py   signaling (same protocol as call.js), one recvonly peer connection per person
#   hooks.py    gets rtcp sender reports + rtp origins out of aiortc
#   sync.py     rtp time --> sender wall clock, so audio and video share one timeline
#   buffer.py   delay estimator, playout clock, video frame queue, audio sample queue
#   player.py   ties it together: schedules playout, reads the test pattern, logs stats
#   pattern.py  test pattern (barcode, flash, beep), shared with call.js
#   outputs.py  opencv window + sound card (or nothing, for headless runs)
