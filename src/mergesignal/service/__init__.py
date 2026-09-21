"""Optional webhook service: receive PR events, analyse, comment.

Importing this package pulls in FastAPI. The CLI imports it lazily inside the
``serve`` command so that ``mergesignal analyze`` never pays for it and works in
environments where the web extras are not installed.
"""
