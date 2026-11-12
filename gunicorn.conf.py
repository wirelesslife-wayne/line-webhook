"""Read automatically by gunicorn at start-up (Render runs `gunicorn app:app`).

Several threads per process, so the review page's 8-second refresh never makes a button
click wait behind it (Wayne 11/12: the desk felt slow). The worker count is left as configured.
"""
threads = 4
